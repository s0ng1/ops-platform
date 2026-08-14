"""SNMP 基础查询：sysDescr / sysObjectID / sysUpTime / sysName，支持 v2c/v3。
基于 pysnmp 7.1 的 v3arch.asyncio 原生协程 API；sync 版本供无线程事件循环的
调用方（to_thread / 同步发现任务）通过 asyncio.run 复用同一套协程核心。
"""
import asyncio

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_cmd,
    get_cmd,
    usmAesCfb128Protocol,
    usmDESPrivProtocol,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
)

SNMP_TIMEOUT = 2
SNMP_RETRIES = 1
# GETBULK 每包行数：walk 用 bulk 比 GETNEXT 逐个省约 25 倍往返与解码开销
SNMP_BULK_REPETITIONS = 25

# SnmpEngine 按事件循环共享（pysnmp 引擎设计为常驻复用，Trap 接收器同款）。
# 绝不能每次调用新建：①引擎持有的 transport 不被 GC（~4MB/次泄漏，2026-07-28 实测
# 20 分钟涨 5GB）；②每次新建都要从磁盘同步加载 pysnmp 自带 MIB 模块（runpy 读文件），
# 在事件循环里阻塞数百毫秒×每调用——23 台设备采集时 API 周期性卡 15~40s 的真凶。
_engines: dict[int, SnmpEngine] = {}


def _get_engine() -> SnmpEngine:
    """取当前事件循环的共享引擎，没有则创建（每个 loop 一个，MIB 只加载一次）。"""
    loop = asyncio.get_running_loop()
    engine = _engines.get(id(loop))
    if engine is None:
        engine = SnmpEngine()
        _engines[id(loop)] = engine
    return engine

OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
OID_SYS_UP_TIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"


def _build_auth(payload: dict):
    """payload 为凭据解密后的 dict。kind=snmp_v2c: {community, port?}；
    kind=snmp_v3: {username, auth_protocol, auth_key, priv_protocol, priv_key, port?}"""
    if payload.get("kind") == "snmp_v2c":
        return CommunityData(payload.get("community", "public"), mpModel=1)

    auth_proto = {
        "SHA": usmHMACSHAAuthProtocol,
        "MD5": usmHMACMD5AuthProtocol,
        "none": usmNoAuthProtocol,
    }.get(payload.get("auth_protocol", "SHA"), usmHMACSHAAuthProtocol)
    priv_proto = {
        "AES": usmAesCfb128Protocol,
        "DES": usmDESPrivProtocol,
        "none": usmNoPrivProtocol,
    }.get(payload.get("priv_protocol", "AES"), usmAesCfb128Protocol)

    kwargs = {}
    if auth_proto is not usmNoAuthProtocol and payload.get("auth_key"):
        kwargs["authKey"] = payload["auth_key"]
        kwargs["authProtocol"] = auth_proto
    else:
        kwargs["authProtocol"] = usmNoAuthProtocol
    if priv_proto is not usmNoPrivProtocol and payload.get("priv_key"):
        kwargs["privKey"] = payload["priv_key"]
        kwargs["privProtocol"] = priv_proto
    else:
        kwargs["privProtocol"] = usmNoPrivProtocol
    return UsmUserData(payload.get("username", ""), **kwargs)


async def _make_target(host: str, payload: dict):
    """按凭据里的端口（默认 161）构造 UDP 目标；create 是协程（含地址解析）。"""
    port = int(payload.get("port", 161))
    return await UdpTransportTarget.create(
        (host, port), timeout=SNMP_TIMEOUT, retries=SNMP_RETRIES
    )


def _err_text(error_status) -> str:
    """7.1 里 errorStatus 可能是 Integer32 或 str，统一取可读文本。"""
    if hasattr(error_status, "prettyPrint"):
        return error_status.prettyPrint()
    return str(error_status)


def _decode_text(raw: bytes, encoding: str) -> str | None:
    """按指定编码解码，含替换符或不可打印字符（如 MAC 等二进制值）返回 None。"""
    decoded = raw.decode(encoding, errors="replace")
    if "�" in decoded:
        return None
    if not all(ch.isprintable() or ch in "\r\n\t" for ch in decoded):
        return None
    return decoded.strip()


def _pretty(val) -> str:
    """取值字符串：含 \\r\\n 等字符的文本会被 prettyPrint 转成 0x 十六进制，
    这种情况尝试解码还原：先 UTF-8（如 H3C 的 sysDescr），失败再试 GBK
    （部分 H3C 的 sysName 是中文按 GBK 编码返回）；都失败说明是二进制值
    （如 MAC 地址），保持 0x 十六进制由调用方解析。"""
    text = val.prettyPrint()
    if text.startswith("0x") and hasattr(val, "asOctets"):
        raw = val.asOctets()
        decoded = _decode_text(raw, "utf-8")
        if decoded is None:
            decoded = _decode_text(raw, "gbk")
        return decoded if decoded is not None else text
    return text


async def _get_values(host: str, payload: dict, oids: list[str]) -> dict[str, str]:
    """协程核心：GET 多个 OID，返回 {OID: 值字符串}。失败抛 RuntimeError。
    引擎用 _get_engine() 共享（见该处注释：新建引擎=内存泄漏+MIB 重复加载双坑）。"""
    target = await _make_target(host, payload)
    errorIndication, errorStatus, _, varbinds = await get_cmd(
        _get_engine(),
        _build_auth(payload),
        target,
        ContextData(),
        *(ObjectType(ObjectIdentity(oid)) for oid in oids),
        lookupMib=False,
    )
    if errorIndication:
        raise RuntimeError(str(errorIndication))
    if errorStatus:
        raise RuntimeError(_err_text(errorStatus))
    return {str(oid): _pretty(val) for oid, val in varbinds}


async def get_system_info(host: str, payload: dict) -> dict:
    """协程 SNMP GET 系统基本信息，失败抛异常由调用方隔离。"""
    oids = [OID_SYS_DESCR, OID_SYS_OBJECT_ID, OID_SYS_UP_TIME, OID_SYS_NAME]
    values = await _get_values(host, payload, oids)
    return {
        "sys_descr": values.get(OID_SYS_DESCR, ""),
        "sys_object_id": values.get(OID_SYS_OBJECT_ID, ""),
        "sys_up_time": values.get(OID_SYS_UP_TIME, ""),
        "sys_name": values.get(OID_SYS_NAME, ""),
    }


def get_system_info_sync(host: str, payload: dict) -> dict:
    """同步包装：在无事件循环的线程里 asyncio.run 跑同一协程核心。"""
    return asyncio.run(get_system_info(host, payload))


async def walk(host: str, payload: dict, base_oid: str) -> dict[str, str]:
    """协程 walk 单个列 OID，返回 {完整OID: 值字符串}。失败抛异常由调用方隔离。
    GETBULK 自实现（maxRepetitions=25）：比 walk_cmd 的 GETNEXT 逐个省 ~25 倍
    往返与包级解码开销（551 口核心交换机单列 6.8s→0.4s 实测），
    显著降低大接口数设备采集期间的事件循环 CPU 占用。
    引擎用 _get_engine() 共享（见该处注释：新建引擎=内存泄漏+MIB 重复加载双坑）。"""
    target = await _make_target(host, payload)
    result: dict[str, str] = {}
    engine = _get_engine()
    auth = _build_auth(payload)
    next_oid = base_oid
    while True:
        prev_oid = next_oid
        errorIndication, errorStatus, _, varbinds = await bulk_cmd(
            engine, auth, target, ContextData(),
            0, SNMP_BULK_REPETITIONS,
            ObjectType(ObjectIdentity(next_oid)),
            lookupMib=False,
        )
        if errorIndication:
            raise RuntimeError(str(errorIndication))
        if errorStatus:
            raise RuntimeError(_err_text(errorStatus))
        in_subtree = False
        for oid, val in varbinds:
            oid_str = str(oid)
            # bulk 返回按字典序后继，出子树说明本列已走完
            if not oid_str.startswith(base_oid + "."):
                in_subtree = False
                break
            result[oid_str] = _pretty(val)
            next_oid = oid_str
            in_subtree = True
        # 收尾：出子树 / 本批不足额（MIB 到头）/ 无进展（防死循环）
        if not in_subtree or len(varbinds) < SNMP_BULK_REPETITIONS or next_oid == prev_oid:
            break
    return result


def walk_sync(host: str, payload: dict, base_oid: str) -> dict[str, str]:
    """同步包装：在无事件循环的线程里 asyncio.run 跑同一协程核心。"""
    return asyncio.run(walk(host, payload, base_oid))


async def get_multi(host: str, payload: dict, oids: list[str]) -> dict[str, str]:
    """协程 GET 多个 OID，返回 {OID: 值}；不存在的 OID 对应值为空串。"""
    raw = await _get_values(host, payload, oids)
    # noSuchObject/noSuchInstance 等异常值按空处理
    return {oid: ("" if "No Such" in text else text) for oid, text in raw.items()}


def get_multi_sync(host: str, payload: dict, oids: list[str]) -> dict[str, str]:
    """同步包装：在无事件循环的线程里 asyncio.run 跑同一协程核心。"""
    return asyncio.run(get_multi(host, payload, oids))


# ---- SNMP Trap 接收（v1/v2c，宽松模式不校验 community）----

OID_SNMP_TRAP = "1.3.6.1.6.3.1.1.4.1.0"  # snmpTrapOID.0（v1 Trap 转换后也在此携带 enterprise OID）


class TrapReceiverHandle:
    """Trap 接收器句柄：close() 释放 UDP 端口与引擎。"""

    def __init__(self, snmp_engine, transport):
        self._engine = snmp_engine
        self._transport = transport

    def close(self) -> None:
        try:
            self._transport.close_transport()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._engine.close_dispatcher()
        except Exception:  # noqa: BLE001
            pass


def start_trap_receiver(port: int, on_trap) -> TrapReceiverHandle:
    """在当前事件循环上启动 Trap 接收（UDP 0.0.0.0:port）。
    on_trap(source_ip, message) 为同步回调（在事件循环内执行），message 为
    "enterprise=<OID> <oid1>=<val1> <oid2>=<val2> ..." 文本。
    宽松模式：任意 community 都接收（统一改写为已注册的占位团体名）。
    """
    from pysnmp.carrier.asyncio.dgram import udp
    from pysnmp.entity import config, engine
    from pysnmp.entity.rfc3413 import ntfrcv
    from pysnmp.proto.rfc1902 import OctetString

    snmp_engine = engine.SnmpEngine()
    transport = udp.UdpTransport().open_server_mode(("0.0.0.0", port))
    config.add_transport(snmp_engine, udp.DOMAIN_NAME, transport)
    # 占位团体名：配合下面的 observer 钩子实现「任意 community 都收」
    config.add_v1_system(snmp_engine, "ops-trap", "public")
    # v1(securityModel=1) / v2c(securityModel=2) 的通知访问控制：全子树放行
    config.add_vacm_user(snmp_engine, 1, "ops-trap", "noAuthNoPriv", (), (), (1, 3, 6, 1))
    config.add_vacm_user(snmp_engine, 2, "ops-trap", "noAuthNoPriv", (), (), (1, 3, 6, 1))

    def _accept_any_community(snmpEngine, execpoint, scope, cb_ctx):
        # rfc2576 安全模型在 _com2sec 前读该 scope；注意必须给 OctetString，
        # 给 str 与内部哈希表键类型不匹配仍会判为未知团体名
        scope["communityName"] = OctetString("public")

    snmp_engine.observer.register_observer(
        _accept_any_community, "rfc2576.processIncomingMsg:writable"
    )

    def _on_notification(snmpEngine, stateReference, contextEngineId, contextName, varBinds, cbCtx):
        try:
            _, addr = snmpEngine.message_dispatcher.get_transport_info(stateReference)
            source_ip = addr[0] if addr else ""
        except Exception:  # noqa: BLE001 - 拿不到对端地址不丢报文
            source_ip = ""
        enterprise = ""
        parts = []
        for oid, val in varBinds:
            oid_text = str(oid)
            val_text = _pretty(val)
            if oid_text == OID_SNMP_TRAP:
                enterprise = val_text
            else:
                parts.append(f"{oid_text}={val_text}")
        message = f"enterprise={enterprise} " + " ".join(parts)
        try:
            on_trap(source_ip, message.strip())
        except Exception:  # noqa: BLE001 - 单报文异常静默不退出
            pass

    ntfrcv.NotificationReceiver(snmp_engine, _on_notification)
    return TrapReceiverHandle(snmp_engine, transport)
