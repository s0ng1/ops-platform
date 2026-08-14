"""中间件轻量探针：Nginx stub_status + Redis INFO（application 设备的两种 probe_kind，零依赖）。
- nginx：HTTP 抓 stub_status 文本，active/reading/writing/waiting 瞬时出点，
  accepts/handled/requests 累计计数器走 rate.py 差值出 *_per_sec（首周期无值）。
- redis：TCP 直连手撸 RESP（有密码先 AUTH，再发 INFO），解析 key:value 文本；
  keyspace_hits/misses 走 rate.py 差值算窗口命中率，ops/s 用 instantaneous_ops_per_sec 瞬时值。
解析纯函数（parse_nginx_status / parse_redis_info）与网络 IO 分离，
网络函数（fetch_nginx_status / fetch_redis_info）可注入替换便于单测。
"""
import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

from ..core.ssrf import ensure_not_blocked

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
# stub_status 页很小，读 64KB 封顶
NGINX_MAX_BODY = 64 * 1024
# INFO 回复正常几十 KB，1MB 封顶防异常长度撑爆内存
REDIS_MAX_REPLY = 1024 * 1024


# ============ nginx stub_status ============

def parse_nginx_status(text: str) -> dict:
    """解析 stub_status 文本，返回 {active, accepts, handled, requests, reading, writing, waiting}。
    缺行容错：解析不出的键不出现在结果里。标准输出形如：
      Active connections: 291
      server accepts handled requests
       16630948 16630948 31070465
      Reading: 6 Writing: 179 Waiting: 106
    """
    out: dict = {}
    m = re.search(r"Active connections:\s*(\d+)", text)
    if m:
        out["active"] = int(m.group(1))
    m = re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s*$", text, re.M)
    if m:
        out["accepts"], out["handled"], out["requests"] = (int(g) for g in m.groups())
    m = re.search(r"Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)", text)
    if m:
        out["reading"], out["writing"], out["waiting"] = (int(g) for g in m.groups())
    return out


async def fetch_nginx_status(config: dict) -> str:
    """HTTP GET stub_status 页返回文本，非 200 抛异常（仅 http，内网 stub_status 一般不开 https）。"""
    timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
    parts = urlsplit(config["url"])
    host = parts.hostname or ""
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    async def _fetch() -> str:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                f"User-Agent: ops-app-probe\r\nConnection: close\r\n\r\n"
            )
            writer.write(req.encode())
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            status_code = int(head.split(b" ", 2)[1])
            if status_code != 200:
                raise ValueError(f"stub_status 返回 HTTP {status_code}")
            body = await reader.read(NGINX_MAX_BODY)
            return body.decode("utf-8", errors="replace")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败忽略
                pass

    return await asyncio.wait_for(_fetch(), timeout)


async def probe_nginx(device_id: int, config: dict, rate_calc=None, fetch_fn=fetch_nginx_status) -> dict:
    """Nginx stub_status 拨测。返回 {available, latency_ms, metrics}；失败抛异常由上层判不可达。"""
    # SSRF 防护：目标命中封禁段（云元数据/链路本地等）则抛异常由上层判不可达
    await ensure_not_blocked(urlsplit(str(config.get("url") or "")).hostname or "")
    t0 = time.monotonic()
    text = await fetch_fn(config)
    latency_ms = (time.monotonic() - t0) * 1000
    parsed = parse_nginx_status(text)
    if not parsed:
        raise ValueError("stub_status 内容无法解析")

    metrics: dict[str, float] = {}
    for name in ("active", "reading", "writing", "waiting"):
        if name in parsed:
            metrics[f"nginx_{name}"] = float(parsed[name])
    # 累计计数器差值出速率，首周期无值
    if rate_calc is not None:
        ts = time.time()
        for name in ("accepts", "handled", "requests"):
            if name not in parsed:
                continue
            rate = rate_calc.rate((device_id, "nginx", name), ts, parsed[name])
            if rate is not None:
                metrics[f"nginx_{name}_per_sec"] = round(rate, 2)
    return {"available": 1, "latency_ms": latency_ms, "metrics": metrics}


# ============ redis INFO ============

def parse_redis_info(text: str) -> dict:
    """解析 INFO 回复文本为 {key: value}（值保持字符串；跳过 # 段标题与空行）。"""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


async def fetch_redis_info(host: str, port: int, password: str | None, timeout: float) -> str:
    """TCP 直连 Redis：有密码先 AUTH（RESP 数组），再发 INFO（内联命令），读批量字符串回复。"""

    async def _fetch() -> str:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            if password:
                pwd = password.encode()
                writer.write(b"*2\r\n$4\r\nAUTH\r\n$%d\r\n%s\r\n" % (len(pwd), pwd))
                await writer.drain()
                reply = await reader.readline()
                if reply.startswith(b"-"):
                    raise ValueError(f"Redis AUTH 失败: {reply.decode(errors='replace').strip()}")
            writer.write(b"INFO\r\n")
            await writer.drain()
            head = await reader.readline()
            if not head.startswith(b"$"):
                raise ValueError(f"Redis INFO 回复异常: {head.decode(errors='replace').strip()}")
            length = int(head[1:].strip())
            if length < 0 or length > REDIS_MAX_REPLY:
                raise ValueError(f"Redis INFO 回复长度异常: {length}")
            data = await reader.readexactly(length)
            return data.decode("utf-8", errors="replace")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败忽略
                pass

    return await asyncio.wait_for(_fetch(), timeout)


async def probe_redis(device_ip: str, device_id: int, config: dict, rate_calc=None,
                      fetch_fn=fetch_redis_info) -> dict:
    """Redis INFO 拨测。host 缺省取设备目标主机；port 缺省 6379；password 可空。
    返回 {available, latency_ms, metrics}；失败抛异常由上层判不可达。
    """
    timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
    host = (config.get("host") or "").strip() or device_ip
    port = int(config.get("port") or 6379)
    password = (config.get("password") or "").strip() or None

    # SSRF 防护：目标命中封禁段（云元数据/链路本地等）则抛异常由上层判不可达
    await ensure_not_blocked(host)

    t0 = time.monotonic()
    text = await fetch_fn(host, port, password, timeout)
    latency_ms = (time.monotonic() - t0) * 1000
    info = parse_redis_info(text)
    if not info:
        raise ValueError("Redis INFO 内容无法解析")

    metrics: dict[str, float] = {}

    def put_int(name: str, key: str) -> None:
        try:
            metrics[name] = float(int(info[key]))
        except (KeyError, TypeError, ValueError):
            pass

    put_int("redis_connected_clients", "connected_clients")
    put_int("redis_used_memory", "used_memory")
    put_int("redis_used_memory_rss", "used_memory_rss")
    # 内存使用率：maxmemory=0（未设上限）时不出点
    try:
        maxmemory = int(info.get("maxmemory") or 0)
        if maxmemory > 0:
            metrics["redis_mem_usage_pct"] = round(int(info["used_memory"]) / maxmemory * 100, 2)
    except (KeyError, TypeError, ValueError):
        pass
    # ops/s 用 Redis 自带瞬时值，不走差值
    try:
        metrics["redis_ops_per_sec"] = float(info["instantaneous_ops_per_sec"])
    except (KeyError, TypeError, ValueError):
        pass
    # 窗口命中率：keyspace_hits/misses 累计计数器差值，首周期无值；窗口内无命令则不出点
    if rate_calc is not None:
        try:
            ts = time.time()
            hits = rate_calc.rate((device_id, "redis", "keyspace_hits"), ts, int(info["keyspace_hits"]))
            misses = rate_calc.rate((device_id, "redis", "keyspace_misses"), ts, int(info["keyspace_misses"]))
            if hits is not None and misses is not None and (hits + misses) > 0:
                metrics["redis_hit_rate"] = round(hits / (hits + misses) * 100, 2)
        except (KeyError, TypeError, ValueError):
            pass
    return {"available": 1, "latency_ms": latency_ms, "metrics": metrics}
