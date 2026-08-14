"""中间件轻量两件套（probe_kind=nginx/redis）：解析纯函数 + 注入 mock 的采集路径 + API 校验 + 内置规则。
网络层不依赖 docker：nginx 走 fetch_fn 注入文本，redis 除注入外另起本地 RESP 假服务器验 AUTH 逻辑。
注意：全测试套件共享一个会话级库——API 建设备 IP 用 192.0.2.8x 段
（203.0.113.x 被 test_ipam/test_logreceiver 占用、192.0.2.9x 被 test_app_probe 占用）。
"""
import asyncio
from types import SimpleNamespace

from conftest import auth

from app.collectors.middleware_probe import (
    fetch_redis_info, parse_nginx_status, parse_redis_info, probe_nginx, probe_redis,
)
from app.collectors.app_probe import collect_app_metrics
from app.collectors.rate import RateCalculator
from app.core.database import SessionLocal
from app.main import _init_db
from app.models import AlertRule
from app.scheduler.scheduler import TASKS


def run(coro):
    return asyncio.run(coro)


def _device(cfg, ip="127.0.0.1", did=9002):
    return SimpleNamespace(id=did, ip=ip, type="application",
                           monitor_enabled=True, probe_config=cfg)


def _by_metric(points):
    return {p.metric: p for p in points}


NGX_1 = (
    "Active connections: 291 \n"
    "server accepts handled requests\n"
    " 16630948 16630948 31070465 \n"
    "Reading: 6 Writing: 179 Waiting: 106 \n"
)
NGX_2 = (
    "Active connections: 300 \n"
    "server accepts handled requests\n"
    " 16631048 16631038 31070665 \n"
    "Reading: 2 Writing: 10 Waiting: 288 \n"
)


def _info(hits=1000, misses=100, maxmemory=100000000, used=50000000, ops=123):
    return (
        "# Server\r\nredis_version:7.2.0\r\n"
        "# Clients\r\nconnected_clients:12\r\n"
        "# Memory\r\n"
        f"used_memory:{used}\r\nused_memory_rss:{used + 1024}\r\nmaxmemory:{maxmemory}\r\n"
        "# Stats\r\n"
        f"instantaneous_ops_per_sec:{ops}\r\nkeyspace_hits:{hits}\r\nkeyspace_misses:{misses}\r\n"
        "# Keyspace\r\ndb0:keys=5,expires=0\r\n"
    )


# ============ nginx 解析纯函数 ============

def test_parse_nginx_status_standard():
    s = parse_nginx_status(NGX_1)
    assert s == {"active": 291, "accepts": 16630948, "handled": 16630948,
                 "requests": 31070465, "reading": 6, "writing": 179, "waiting": 106}


def test_parse_nginx_status_missing_lines():
    # 只有 Active 行：只出 active，其余键不缺省补 0
    assert parse_nginx_status("Active connections: 7 \n") == {"active": 7}
    # 全是噪音：空 dict（上层判解析失败 → 不可达）
    assert parse_nginx_status("<html>404</html>") == {}
    # 缺 Reading 行：计数器行照常解析
    s = parse_nginx_status("Active connections: 1 \n 10 20 30 \n")
    assert s == {"active": 1, "accepts": 10, "handled": 20, "requests": 30}


# ============ nginx 采集（注入 fetch_fn）============

def _nginx_probe_fn(text_or_exc):
    """构造注入 collect_app_metrics 的 probe_nginx_fn：内部走真 probe_nginx，网络层换掉。"""
    async def fake_fetch(cfg):
        if isinstance(text_or_exc, Exception):
            raise text_or_exc
        return text_or_exc

    async def _fn(did, cfg, rc):
        return await probe_nginx(did, cfg, rc, fetch_fn=fake_fetch)
    return _fn


def test_nginx_collect_two_cycles_rates():
    """两周期：首周期只出瞬时值，第二周期出 accepts/handled/requests 速率。"""
    rc = RateCalculator()
    cfg = {"probe_kind": "nginx", "url": "http://127.0.0.1/nginx_status"}
    dev = _device(cfg)

    points = run(collect_app_metrics(dev, probe_nginx_fn=_nginx_probe_fn(NGX_1), rate_calc=rc))
    m = _by_metric(points)
    assert m["app_available"].value == 1.0
    assert m["app_latency"].value >= 0
    assert m["nginx_active"].value == 291.0
    assert m["nginx_reading"].value == 6.0
    assert m["nginx_writing"].value == 179.0
    assert m["nginx_waiting"].value == 106.0
    assert "nginx_requests_per_sec" not in m  # 首周期无速率
    assert all(p.labels == {"probe_kind": "nginx"} for p in points)

    points = run(collect_app_metrics(dev, probe_nginx_fn=_nginx_probe_fn(NGX_2), rate_calc=rc))
    m = _by_metric(points)
    assert m["nginx_accepts_per_sec"].value > 0
    assert m["nginx_handled_per_sec"].value > 0
    assert m["nginx_requests_per_sec"].value > 0


def test_nginx_failure_unavailable():
    """抓取失败/内容不可解析：只出 app_available=0（「应用不可达」规则照常生效）。"""
    cfg = {"probe_kind": "nginx", "url": "http://127.0.0.1/nginx_status"}
    points = run(collect_app_metrics(
        _device(cfg), probe_nginx_fn=_nginx_probe_fn(ConnectionRefusedError("refused"))))
    assert len(points) == 1 and points[0].metric == "app_available" and points[0].value == 0.0
    # 200 但内容不是 stub_status
    points = run(collect_app_metrics(_device(cfg), probe_nginx_fn=_nginx_probe_fn("<html></html>")))
    assert len(points) == 1 and points[0].value == 0.0


# ============ redis 解析纯函数 ============

def test_parse_redis_info():
    info = parse_redis_info(_info())
    assert info["connected_clients"] == "12"
    assert info["used_memory"] == "50000000"
    assert info["keyspace_hits"] == "1000"
    assert info["db0"] == "keys=5,expires=0"
    assert "# Server" not in info
    assert parse_redis_info("") == {}


# ============ redis 采集（注入 fetch_fn）============

def _redis_probe_fn(text_or_exc):
    async def fake_fetch(host, port, password, timeout):
        if isinstance(text_or_exc, Exception):
            raise text_or_exc
        return text_or_exc

    async def _fn(ip, did, cfg, rc):
        return await probe_redis(ip, did, cfg, rc, fetch_fn=fake_fetch)
    return _fn


def test_redis_collect_two_cycles_hit_rate():
    """两周期：首周期无命中率，第二周期窗口命中率 = hits差/(hits+misses差)。"""
    rc = RateCalculator()
    cfg = {"probe_kind": "redis"}
    dev = _device(cfg)

    points = run(collect_app_metrics(dev, probe_redis_fn=_redis_probe_fn(_info()), rate_calc=rc))
    m = _by_metric(points)
    assert m["app_available"].value == 1.0
    assert m["redis_connected_clients"].value == 12.0
    assert m["redis_used_memory"].value == 50000000.0
    assert m["redis_used_memory_rss"].value == 50001024.0
    assert m["redis_mem_usage_pct"].value == 50.0
    assert m["redis_ops_per_sec"].value == 123.0  # 瞬时值不走差值，首周期即有
    assert "redis_hit_rate" not in m  # 首周期无窗口命中率
    assert all(p.labels == {"probe_kind": "redis"} for p in points)

    points = run(collect_app_metrics(
        dev, probe_redis_fn=_redis_probe_fn(_info(hits=1090, misses=110)), rate_calc=rc))
    m = _by_metric(points)
    assert m["redis_hit_rate"].value == 90.0


def test_redis_no_maxmemory_no_pct():
    """maxmemory=0（未设上限）不出内存使用率点。"""
    points = run(collect_app_metrics(
        _device({"probe_kind": "redis"}),
        probe_redis_fn=_redis_probe_fn(_info(maxmemory=0)), rate_calc=RateCalculator()))
    m = _by_metric(points)
    assert m["app_available"].value == 1.0
    assert "redis_mem_usage_pct" not in m


def test_redis_failure_unavailable():
    cfg = {"probe_kind": "redis"}
    points = run(collect_app_metrics(
        _device(cfg), probe_redis_fn=_redis_probe_fn(ConnectionRefusedError("refused"))))
    assert len(points) == 1 and points[0].metric == "app_available" and points[0].value == 0.0


# ============ redis RESP 协议真联（本地假服务器，验 AUTH 逻辑）============

def test_redis_fetch_auth_and_info_over_tcp():
    async def main():
        received = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            """假 Redis：AUTH 密码 s3cret，INFO 回固定文本；记录收到的命令供断言。"""
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    if line.startswith(b"*"):  # RESP 数组（AUTH）
                        n = int(line[1:])
                        parts = []
                        for _ in range(n):
                            await reader.readline()  # $len 行
                            parts.append((await reader.readline()).strip())
                        received.append(parts[0].upper().decode())
                        if parts[0].upper() == b"AUTH" and len(parts) > 1 and parts[1] == b"s3cret":
                            writer.write(b"+OK\r\n")
                        else:
                            writer.write(b"-ERR invalid password\r\n")
                    else:  # 内联命令（INFO）
                        received.append(line.strip().upper().decode())
                        body = _info().encode()
                        writer.write(b"$%d\r\n%s\r\n" % (len(body), body))
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            # 正确密码：AUTH + INFO 都发出，拿到文本
            text = await fetch_redis_info("127.0.0.1", port, "s3cret", 2)
            assert "connected_clients:12" in text
            assert received[:2] == ["AUTH", "INFO"]
            # 错误密码：AUTH 失败抛异常
            received.clear()
            try:
                await fetch_redis_info("127.0.0.1", port, "wrong", 2)
                raise AssertionError("错误密码应当抛异常")
            except ValueError as e:
                assert "AUTH" in str(e)
            # 无密码：不发 AUTH 直接 INFO
            received.clear()
            text = await fetch_redis_info("127.0.0.1", port, None, 2)
            assert "connected_clients:12" in text
            assert received == ["INFO"]
    run(main())


# ============ 空配置容错（不回归）============

def test_middleware_empty_config_tolerated():
    assert run(collect_app_metrics(_device({"probe_kind": "nginx"}))) == []  # 缺 url
    # redis 无必填项：连不上只出 app_available=0，不抛异常
    cfg = {"probe_kind": "redis", "port": 6399, "timeout": 1}  # 6399 没人监听
    points = run(collect_app_metrics(_device(cfg)))
    assert len(points) == 1 and points[0].metric == "app_available" and points[0].value == 0.0


# ============ 调度注册（不回归）============

def test_scheduler_task_still_registered():
    task = next((t for t in TASKS if t.name == "app_probe"), None)
    assert task is not None and task.interval == 60
    assert task.applies_to(SimpleNamespace(type="application", monitor_enabled=True))


# ============ 内置规则 ============

def test_builtin_redis_rule_seeded(client):
    db = SessionLocal()
    try:
        db.query(AlertRule).filter(AlertRule.name == "Redis 内存使用率过高").delete()
        db.commit()
    finally:
        db.close()
    _init_db()
    db = SessionLocal()
    try:
        rules = db.query(AlertRule).filter(AlertRule.name == "Redis 内存使用率过高").all()
        # 既有的「应用不可达」不受影响
        avail = db.query(AlertRule).filter(AlertRule.name == "应用不可达").all()
    finally:
        db.close()
    assert len(rules) == 1
    r = rules[0]
    assert (r.metric, r.op, r.threshold, r.duration_cycles, r.severity, r.device_type) == \
        ("redis_mem_usage_pct", ">", 90, 2, "warning", "application")
    assert r.builtin is True
    assert len(avail) == 1


# ============ API：probe_config 校验 ============

def _create(client, token, ip, probe_config):
    return client.post("/api/devices", json={
        "ip": ip, "name": f"mw-{ip}", "type": "application", "probe_config": probe_config,
    }, headers=auth(token))


def test_api_nginx_validation(client, admin_token):
    # 缺 url / 非 http(s) scheme
    assert _create(client, admin_token, "192.0.2.81", {"probe_kind": "nginx"}).status_code == 422
    assert _create(client, admin_token, "192.0.2.81",
                   {"probe_kind": "nginx", "url": "ftp://x/status"}).status_code == 422
    # 合法配置能建
    r = _create(client, admin_token, "192.0.2.81",
                {"probe_kind": "nginx", "url": "http://192.0.2.81/nginx_status", "timeout": 3})
    assert r.status_code == 201, r.text
    assert r.json()["probe_config"]["probe_kind"] == "nginx"


def test_api_redis_validation(client, admin_token):
    # 端口非法
    assert _create(client, admin_token, "192.0.2.82",
                   {"probe_kind": "redis", "port": 70000}).status_code == 422
    assert _create(client, admin_token, "192.0.2.82",
                   {"probe_kind": "redis", "port": 0}).status_code == 422
    # 端口可空（缺省 6379）；host/password 可空
    r = _create(client, admin_token, "192.0.2.82", {"probe_kind": "redis", "password": "x"})
    assert r.status_code == 201, r.text
    r = _create(client, admin_token, "192.0.2.83",
                {"probe_kind": "redis", "host": "192.0.2.83", "port": 6380, "timeout": 5})
    assert r.status_code == 201, r.text
