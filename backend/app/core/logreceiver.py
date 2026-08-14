"""Syslog / SNMP Trap 接收器：UDP 收报 → 解析 → 落 log_events → 规则匹配 → 告警。
挂 lifespan 常驻（同 monitor_loop 模式）；单报文异常静默，不阻塞接收循环。
同步 DB 操作走 to_thread 短会话；告警走引擎指标点路径（同「配置变更」模式：
产出 log_event 指标点，由内置「日志事件-x」规则按 labels.severity 分级触发）。
"""
import asyncio
import ipaddress
import logging
import re
import time
from types import SimpleNamespace

from ..collectors.snmp_metrics import MetricPoint
from ..models import Device, LogEvent, LogRule
from .config import get_settings
from .database import SessionLocal

log = logging.getLogger(__name__)

SUPPRESS_SECONDS = 300  # 同一规则同一来源 5 分钟内重复命中只告一次

# {(rule_id, source_ip): 上次触发告警的 monotonic 时间}，进程内存即可（重启重新计）
_last_alert: dict[tuple[int, str], float] = {}

_PRI_RE = re.compile(r"^<(\d{1,3})>")
_TS_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+")


def parse_syslog(data: bytes) -> dict:
    """解析 RFC3164 报文：<pri>时间戳 主机 tag: 消息（pri = facility*8 + severity）。
    返回 {facility, severity, host, message}；message 保留 tag 段（规则关键字常打在 tag 上，
    如厂商的模块/助记符），结构不符宽松兜底存原文。"""
    raw = data.decode("utf-8", errors="replace").strip("\x00\r\n")
    result = {"facility": None, "severity": None, "host": "", "message": raw}
    try:
        text = raw
        m = _PRI_RE.match(text)
        if m:
            pri = int(m.group(1))
            if 0 <= pri <= 191:
                result["facility"] = pri // 8
                result["severity"] = pri % 8
            text = text[m.end():]
        m = _TS_RE.match(text)
        if m:
            text = text[m.end():]
            parts = text.split(None, 1)
            if parts:
                result["host"] = parts[0]
                text = parts[1] if len(parts) > 1 else ""
        if text.strip():
            result["message"] = text.strip()
    except Exception:  # noqa: BLE001 - 兜底存原文
        result.update(facility=None, severity=None, host="", message=raw)
    return result


def rule_matches(rule, kind: str, source_ip: str, severity: int | None, message: str) -> bool:
    """判断日志事件是否命中规则（条件与关系）。
    severity_lte 仅适用于 syslog；trap 或无等级报文不命中带等级条件的规则。"""
    if rule.source_ip and rule.source_ip != source_ip:
        return False
    if rule.keyword and rule.keyword not in message:
        return False
    if rule.severity_lte is not None and (severity is None or severity > rule.severity_lte):
        return False
    return True


def _store_event(source_ip: str, kind: str, facility, severity, message: str) -> int | None:
    """短会话落库一条日志事件，返回 id。同步函数，供 to_thread 调用（测试注入点）。"""
    db = SessionLocal()
    try:
        event = LogEvent(
            source_ip=source_ip, kind=kind, facility=facility,
            severity=severity, message=message,
        )
        db.add(event)
        db.commit()
        return event.id
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("日志事件入库失败")
        return None
    finally:
        db.close()


def _load_enabled_rules() -> list[SimpleNamespace]:
    """短会话读全部启用规则（会话关闭后仍可用）。供 to_thread 调用。"""
    db = SessionLocal()
    try:
        return [
            SimpleNamespace(
                id=r.id, name=r.name, source_ip=r.source_ip, keyword=r.keyword,
                severity_lte=r.severity_lte, alert_severity=r.alert_severity,
            )
            for r in db.query(LogRule).filter(LogRule.enabled.is_(True)).all()
        ]
    except Exception:  # noqa: BLE001
        log.exception("日志规则读取失败")
        return []
    finally:
        db.close()


def _find_device_id(source_ip: str) -> int | None:
    """按来源 IP 找已入库设备（告警事件必须挂设备）。供 to_thread 调用。"""
    db = SessionLocal()
    try:
        device = db.query(Device).filter(Device.ip == source_ip).first()
        return device.id if device else None
    finally:
        db.close()


async def _fire_log_alert(rule, source_ip: str, message: str) -> None:
    """命中规则：产出 log_event 指标点交告警引擎（同「配置变更」模式）。
    来源 IP 未入库设备时只存日志事件、不产生告警。"""
    device_id = await asyncio.to_thread(_find_device_id, source_ip)
    if device_id is None:
        log.debug("日志来源 %s 未入库设备，跳过告警（规则 %s）", source_ip, rule.name)
        return
    point = MetricPoint(
        device_id,
        "log_event",
        1.0,
        {"severity": rule.alert_severity, "rule": rule.name, "msg": message[:80]},
    )
    from ..alerting import engine as alert_engine

    await alert_engine.evaluate_points([point])


async def ingest_event(
    source_ip: str,
    kind: str,
    facility: int | None,
    severity: int | None,
    message: str,
    store=None,
) -> None:
    """单条日志事件处理入口：落库 → 规则匹配 → 命中产告警。任何异常静默。
    store 可注入（默认 _store_event），便于测试隔离落库。"""
    try:
        store = store or _store_event
        await asyncio.to_thread(store, source_ip, kind, facility, severity, message)
        for rule in await asyncio.to_thread(_load_enabled_rules):
            if not rule_matches(rule, kind, source_ip, severity, message):
                continue
            key = (rule.id, source_ip)
            now = time.monotonic()
            if now - _last_alert.get(key, -SUPPRESS_SECONDS * 2) < SUPPRESS_SECONDS:
                continue
            _last_alert[key] = now
            await _fire_log_alert(rule, source_ip, message)
    except Exception:  # noqa: BLE001 - 单报文异常静默不退出
        log.exception("日志事件处理失败 kind=%s source=%s", kind, source_ip)


def reset_suppression() -> None:
    """清空告警抑制记录（测试用）。"""
    _last_alert.clear()


# ---- 来源 IP 白名单 ----

# 模块级白名单（start_log_receivers 时从 settings 加载一次；空=全部接收）
_syslog_nets: list = []
_trap_nets: list = []


def _parse_allowlist(raw: str) -> list:
    """解析逗号/分号分隔的 IP/CIDR 白名单，非法项记日志忽略。"""
    nets = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("忽略非法白名单项 %s", part)
    return nets


def _source_allowed(ip_str: str, nets: list) -> bool:
    """白名单为空=全部放行；非空=来源必须命中其一。"""
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in nets)


class _SyslogProtocol(asyncio.DatagramProtocol):
    """Syslog UDP 接收：逐报文解析后异步落库，单报文异常不影响后续接收。"""

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            if not _source_allowed(addr[0], _syslog_nets):
                log.debug("Syslog 来源 %s 不在白名单，丢弃", addr[0])
                return
            parsed = parse_syslog(data)
            asyncio.get_running_loop().create_task(
                ingest_event(
                    addr[0], "syslog",
                    parsed["facility"], parsed["severity"], parsed["message"],
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("Syslog 报文处理失败")


def _on_trap(source_ip: str, message: str) -> None:
    """Trap 回调（在事件循环内执行）：转异步处理入口。"""
    try:
        if not _source_allowed(source_ip, _trap_nets):
            log.debug("Trap 来源 %s 不在白名单，丢弃", source_ip)
            return
        asyncio.get_running_loop().create_task(ingest_event(source_ip, "trap", None, None, message))
    except Exception:  # noqa: BLE001
        log.exception("Trap 报文处理失败")


class LogReceiverHandle:
    """接收器句柄：close() 释放两个 UDP 端口。"""

    def __init__(self):
        self.syslog_transport = None
        self.trap = None

    def close(self) -> None:
        if self.syslog_transport is not None:
            try:
                self.syslog_transport.close()
            except Exception:  # noqa: BLE001
                pass
        if self.trap is not None:
            self.trap.close()


async def start_log_receivers() -> LogReceiverHandle:
    """启动 Syslog / Trap 接收器；单个启动失败静默降级，另一个照常。"""
    global _syslog_nets, _trap_nets
    settings = get_settings()
    _syslog_nets = _parse_allowlist(settings.syslog_allow)
    _trap_nets = _parse_allowlist(settings.trap_allow)
    handle = LogReceiverHandle()
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            _SyslogProtocol, local_addr=("0.0.0.0", settings.syslog_port)
        )
        handle.syslog_transport = transport
        log.info("Syslog 接收器已监听 UDP %d", settings.syslog_port)
    except Exception:  # noqa: BLE001 - 端口被占等不阻塞主程序
        log.exception("Syslog 接收器启动失败（UDP %d）", settings.syslog_port)
    try:
        from ..collectors import snmp

        handle.trap = snmp.start_trap_receiver(settings.trap_port, _on_trap)
        log.info("SNMP Trap 接收器已监听 UDP %d", settings.trap_port)
    except Exception:  # noqa: BLE001
        log.exception("Trap 接收器启动失败（UDP %d）", settings.trap_port)
    return handle
