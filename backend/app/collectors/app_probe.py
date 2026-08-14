"""应用仿真拨测采集器：http/dns/tcp 探活 + nginx/redis 中间件轻量指标，纯 asyncio 实现、零新依赖。
设备 type=application，拨测配置存 device.probe_config（JSON）：
- http/https: {"probe_kind":"http","url":..., "expect_status":可空, "keyword":可空, "timeout":5}
- dns:        {"probe_kind":"dns","domain":..., "expect_ip":可空, "server":可空, "timeout":5}
- tcp:        {"probe_kind":"tcp","port":..., "banner":可空, "timeout":5}
- nginx:      {"probe_kind":"nginx","url":stub_status 地址, "timeout":5}（见 middleware_probe）
- redis:      {"probe_kind":"redis","host":可空默认目标主机,"port":6379,"password":可空, "timeout":5}
公共指标：app_available(0/1)、app_latency(ms)（可用性口径统一，「应用不可达」规则对五种 kind 都生效），
http 另出 app_status_code，nginx/redis 另出 nginx_*/redis_* 指标；labels 只带 probe_kind。
注入点：collect_app_metrics 的 probe_* 参数可替换，便于 mock 单测。
"""
import asyncio
import logging
import ssl
import struct
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..core.ssrf import ensure_not_blocked
from ..models import Device
from .middleware_probe import probe_nginx, probe_redis
from .snmp_metrics import MetricPoint

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
# 响应体只读前 64KB（够做关键字匹配，避免大页面拖垮采集）
HTTP_MAX_BODY = 64 * 1024
TCP_BANNER_BYTES = 512


# ============ http/https ============

async def probe_http(config: dict) -> dict:
    """手写最小 HTTP/1.1 GET。返回 {available, latency_ms, status_code}。
    期望状态码缺省 200~399 算活；配了关键字还要命中响应体才算活。
    https 不验证证书（内网自签常见）。
    """
    timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
    url = config["url"]
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    # SSRF 防护：目标解析后命中封禁段（回环/元数据等）则抛 SSRFBlockedError，由采集入口转不可达
    await ensure_not_blocked(host)

    t0 = time.monotonic()
    ssl_ctx = None
    if parts.scheme == "https":
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    async def _fetch() -> tuple[int, bytes]:
        reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
        try:
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                f"User-Agent: ops-app-probe\r\nConnection: close\r\n\r\n"
            )
            writer.write(req.encode())
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            status_code = int(head.split(b" ", 2)[1])
            body = await reader.read(HTTP_MAX_BODY + 1)
            return status_code, body[:HTTP_MAX_BODY]
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败忽略
                pass

    status_code, body = await asyncio.wait_for(_fetch(), timeout)
    latency_ms = (time.monotonic() - t0) * 1000

    expect = config.get("expect_status")
    ok = status_code == int(expect) if expect is not None else 200 <= status_code < 400
    keyword = (config.get("keyword") or "").strip()
    if ok and keyword:
        ok = keyword.encode() in body
    return {"available": 1 if ok else 0, "latency_ms": latency_ms, "status_code": status_code}


# ============ dns ============

def _build_dns_query(domain: str, qid: int) -> bytes:
    """标准 DNS 查询包（A 记录，RD=1）。"""
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    question = b"".join(
        bytes([len(label)]) + label.encode() for label in domain.rstrip(".").split(".")
    ) + b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + question


def _parse_dns_answers(data: bytes, qid: int) -> list[str]:
    """解析应答包里的 A 记录，返回 IP 列表。解析失败抛异常。"""
    rid, flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])
    if rid != qid or not (flags & 0x8000):
        raise ValueError("非本查询的应答")
    if flags & 0x000F:  # RCODE 非 0（如 NXDOMAIN）
        return []
    offset = 12

    def skip_name(pos: int) -> int:
        while True:
            length = data[pos]
            if length == 0:
                return pos + 1
            if length & 0xC0 == 0xC0:  # 压缩指针
                return pos + 2
            pos += 1 + length

    for _ in range(qdcount):  # 跳过问题段
        offset = skip_name(offset) + 4
    ips: list[str] = []
    for _ in range(ancount):
        offset = skip_name(offset)
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        if rtype == 1 and rdlength == 4:  # A 记录
            ips.append(".".join(str(b) for b in data[offset:offset + 4]))
        offset += rdlength
    return ips


async def _query_dns_server(server: str, port: int, domain: str, timeout: float) -> list[str]:
    """向指定 DNS 服务器发 UDP 查询并解析 A 记录应答。"""
    qid = int(time.monotonic() * 1000) % 0xFFFF
    packet = _build_dns_query(domain, qid)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bytes] = loop.create_future()

    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not fut.done():
                fut.set_result(data)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)

    transport, _ = await loop.create_datagram_endpoint(
        _Proto, remote_addr=(server, port)
    )
    try:
        transport.sendto(packet)
        data = await asyncio.wait_for(fut, timeout)
    finally:
        transport.close()
    return _parse_dns_answers(data, qid)


async def probe_dns(config: dict) -> dict:
    """域名解析拨测。返回 {available, latency_ms}。
    未配 server 走系统解析（getaddrinfo）；配了 server 手写 UDP 查询。
    配了 expect_ip 时解析结果须包含该 IP 才算活。
    """
    timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
    domain = config["domain"]
    server = (config.get("server") or "").strip()

    t0 = time.monotonic()
    if server:
        # server 支持 host 或 host:port（非标端口便于测试/内网转发场景）
        shost, _, sport = server.partition(":")
        ips = await asyncio.wait_for(
            _query_dns_server(shost, int(sport) if sport else 53, domain, timeout), timeout
        )
    else:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(domain, None, family=2), timeout  # AF_INET
        )
        ips = [info[4][0] for info in infos]
    latency_ms = (time.monotonic() - t0) * 1000

    expect_ip = (config.get("expect_ip") or "").strip()
    ok = bool(ips) and (not expect_ip or expect_ip in ips)
    return {"available": 1 if ok else 0, "latency_ms": latency_ms}


# ============ tcp ============

async def probe_tcp(host: str, config: dict) -> dict:
    """TCP 连通拨测。返回 {available, latency_ms}。
    配了 banner 时连接后读一小段，须包含该子串才算活（读不到也算不匹配）。
    """
    timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
    port = int(config["port"])
    banner = (config.get("banner") or "").strip()

    # SSRF 防护：目标命中封禁段则抛异常，由采集入口转不可达
    await ensure_not_blocked(host)

    t0 = time.monotonic()

    async def _connect() -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            if banner:
                try:
                    return await asyncio.wait_for(reader.read(TCP_BANNER_BYTES), timeout)
                except asyncio.TimeoutError:
                    return b""
            return b""
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败忽略
                pass

    data = await asyncio.wait_for(_connect(), timeout)
    latency_ms = (time.monotonic() - t0) * 1000
    ok = True if not banner else banner.encode() in data
    return {"available": 1 if ok else 0, "latency_ms": latency_ms}


# ============ 采集入口 ============

async def collect_app_metrics(
    device: Device,
    probe_http_fn=probe_http,
    probe_dns_fn=probe_dns,
    probe_tcp_fn=probe_tcp,
    probe_nginx_fn=probe_nginx,
    probe_redis_fn=probe_redis,
    rate_calc=None,
) -> list[MetricPoint]:
    """采集一台 application 设备的拨测指标。配置缺失/异常静默回退（只记日志）。
    rate_calc 仅 nginx/redis 的累计计数器差值用，缺省 None 时不出速率类指标。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    did = device.id
    cfg = device.probe_config or {}
    kind = cfg.get("probe_kind")
    labels = {"probe_kind": str(kind or "")}

    def unavailable() -> list[MetricPoint]:
        # 拨测失败/超时：可用性 0（时延不出点，无意义）
        return [MetricPoint(did, "app_available", 0.0, labels, now)]

    try:
        if kind == "http":
            if not cfg.get("url"):
                log.debug("application 设备 %s 未配置拨测 URL", device.ip)
                return []
            result = await probe_http_fn(cfg)
        elif kind == "dns":
            if not cfg.get("domain"):
                log.debug("application 设备 %s 未配置拨测域名", device.ip)
                return []
            result = await probe_dns_fn(cfg)
        elif kind == "tcp":
            if cfg.get("port") is None:
                log.debug("application 设备 %s 未配置拨测端口", device.ip)
                return []
            result = await probe_tcp_fn(device.ip, cfg)
        elif kind == "nginx":
            if not cfg.get("url"):
                log.debug("application 设备 %s 未配置 stub_status URL", device.ip)
                return []
            result = await probe_nginx_fn(did, cfg, rate_calc)
        elif kind == "redis":
            # host/port/password 都有缺省值，无需前置校验
            result = await probe_redis_fn(device.ip, did, cfg, rate_calc)
        else:
            log.debug("application 设备 %s 拨测配置为空或类型未知：%r", device.ip, kind)
            return []
    except Exception as e:  # noqa: BLE001 - 拨测失败即不可达
        log.debug("应用拨测失败 %s (%s): %s", device.ip, kind, e)
        return unavailable()

    points = [
        MetricPoint(did, "app_available", float(result["available"]), labels, now),
        MetricPoint(did, "app_latency", round(result["latency_ms"], 1), labels, now),
    ]
    if kind == "http" and result.get("status_code") is not None:
        points.append(MetricPoint(did, "app_status_code", float(result["status_code"]), labels, now))
    # nginx/redis 的中间件指标（解析失败的单键已在探针内跳过）
    for metric, value in (result.get("metrics") or {}).items():
        points.append(MetricPoint(did, metric, float(value), labels, now))
    return points
