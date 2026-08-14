"""报表 API 测试：可用率、接口流量日/月报、top 截断、空数据、xlsx 导出。
注意：PG 上 metrics 有 30 天保留策略（timescale.py），播种日期必须取最近几天，
否则会被 retention 任务 DROP chunk（历史上踩过：固定历史日期在 PG 上被查空）。
播种点间隔 ≥1 小时：保证连续聚合每 5 分钟桶只含 1 个原始点，
桶均值 == 原始值，双方言（cagg SQL 聚合 / SQLite Python 聚合）断言值一致。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.models import Device, Metric
from conftest import auth


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# 相对当前时间取日期：D1=前天、D2=昨天（均在 30 天保留窗口内；
# 其他用例以 utcnow() 写入的 if_in_bps 在今天，不会落入 D1 的过滤范围）
BASE = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
D1, D2 = BASE - timedelta(days=2), BASE - timedelta(days=1)
# 月报需要另一个月份的点（必须不同于 D1 所在月）：优先 D1 上月末；若它相对今天
# 超出 25 天（月末跑测时可能被 30 天 retention 清掉），改用 D1 次月（未来点不受影响）
_first_of_d1_month = D1.replace(day=1)
_prev = _first_of_d1_month - timedelta(days=1)
OTHER_MONTH = _prev if (BASE - _prev).days <= 25 else _first_of_d1_month + timedelta(days=32)
D1S, D2S = D1.date().isoformat(), D2.date().isoformat()


def _put(db, did, metric, value, when, labels=None):
    db.add(Metric(time=when, device_id=did, metric=metric, value=float(value), labels=labels or {}))


@pytest.fixture(scope="session")
def report_data(client):
    """两台设备 + 已知 device_online / if_in_out_bps 数据。"""
    db = SessionLocal()
    try:
        sw = Device(name="报表核心交换机", ip="10.99.0.1", type="network")
        srv = Device(name="报表Linux服务器", ip="10.99.0.2", type="server_linux")
        db.add_all([sw, srv])
        db.flush()
        # 可用率：sw D1 四点上 3 → 0.75；sw D2 两点全上 → 1.0；srv D1 上 1/2 → 0.5
        for h, v in zip((0, 6, 12, 18), (1, 1, 1, 0)):
            _put(db, sw.id, "device_online", v, D1.replace(hour=h))
        for h in (0, 12):
            _put(db, sw.id, "device_online", 1, D2.replace(hour=h))
        for h, v in zip((0, 12), (1, 0)):
            _put(db, srv.id, "device_online", v, D1.replace(hour=h))
        # 流量 sw GE0/0/1 @D1：in [100,200,300,400] out [10,20,30,40]
        # p95 两侧 8 值 [10..400] inclusive 插值 = 365
        for h, i, o in zip((0, 6, 12, 18), (100, 200, 300, 400), (10, 20, 30, 40)):
            _put(db, sw.id, "if_in_bps", i, D1.replace(hour=h), {"if": "GE0/0/1"})
            _put(db, sw.id, "if_out_bps", o, D1.replace(hour=h), {"if": "GE0/0/1"})
        # GE0/0/2 @D1：均值更高，top=1 应只留它
        for h, i, o in zip((0, 12), (1000, 2000), (500, 1500)):
            _put(db, sw.id, "if_in_bps", i, D1.replace(hour=h), {"if": "GE0/0/2"})
            _put(db, sw.id, "if_out_bps", o, D1.replace(hour=h), {"if": "GE0/0/2"})
        # GE0/0/1 @另一月一个点：月报应分出两个周期
        _put(db, sw.id, "if_in_bps", 800, OTHER_MONTH, {"if": "GE0/0/1"})
        _put(db, sw.id, "if_out_bps", 80, OTHER_MONTH, {"if": "GE0/0/1"})
        db.commit()
        sw_id, srv_id = sw.id, srv.id
    finally:
        db.close()
    if engine.dialect.name == "postgresql":
        # cagg 策略只定时刷最近窗口，测试数据需手动刷新；CALL 不能在事务块里跑，需 AUTOCOMMIT。
        # cagg 未建成（TimescaleDB 缺失）则静默跳过，报表走原始表回退
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("CALL refresh_continuous_aggregate('metrics_5m', NULL, NULL)"))
        except Exception:  # noqa: BLE001
            pass
    return {"sw": sw_id, "srv": srv_id}


def test_availability_basic(client, admin_token, report_data):
    r = client.get(f"/api/reports/availability?start={D1S}&end={D2S}",
                   headers=auth(admin_token))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    sw, srv = report_data["sw"], report_data["srv"]
    by_key = {(x["device_id"], x["day"]): x for x in rows}
    assert set(by_key) == {(sw, D1S), (sw, D2S), (srv, D1S)}
    a = by_key[(sw, D1S)]
    assert a["device_name"] == "报表核心交换机" and a["ip"] == "10.99.0.1" and a["type"] == "network"
    assert a["total_points"] == 4 and a["online_points"] == 3
    assert a["availability"] == pytest.approx(0.75)
    assert by_key[(sw, D2S)]["availability"] == pytest.approx(1.0)
    b = by_key[(srv, D1S)]
    assert b["total_points"] == 2 and b["availability"] == pytest.approx(0.5)


def test_availability_filters(client, admin_token, report_data):
    r = client.get(f"/api/reports/availability?start={D1S}&end={D2S}&device_type=server_linux",
                   headers=auth(admin_token))
    rows = r.json()["rows"]
    assert {x["device_id"] for x in rows} == {report_data["srv"]}
    r = client.get(f"/api/reports/availability?start={D1S}&end={D2S}&device_id={report_data['sw']}",
                   headers=auth(admin_token))
    assert {x["device_id"] for x in r.json()["rows"]} == {report_data["sw"]}


def test_availability_empty(client, admin_token, report_data):
    r = client.get("/api/reports/availability?start=2020-01-01&end=2020-01-02",
                   headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["rows"] == []


def test_traffic_day(client, admin_token, report_data):
    # end 纯日期含当天
    r = client.get(f"/api/reports/traffic?start={D1S}&end={D1S}", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2  # GE0/0/1 + GE0/0/2，无其他设备混入
    by_if = {x["interface"]: x for x in rows}
    g1 = by_if["GE0/0/1"]
    assert g1["device_name"] == "报表核心交换机" and g1["period"] == D1S
    assert g1["in_avg"] == pytest.approx(250.0) and g1["in_max"] == pytest.approx(400.0)
    assert g1["out_avg"] == pytest.approx(25.0) and g1["out_max"] == pytest.approx(40.0)
    assert g1["p95"] == pytest.approx(365.0)
    assert by_if["GE0/0/2"]["in_avg"] == pytest.approx(1500.0)


def test_traffic_month(client, admin_token, report_data):
    start = min(D1, OTHER_MONTH).date().isoformat()
    end = max(D2, OTHER_MONTH).date().isoformat()
    r = client.get(f"/api/reports/traffic?start={start}&end={end}&granularity=month"
                   f"&device_id={report_data['sw']}", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    rows = [x for x in r.json()["rows"] if x["interface"] == "GE0/0/1"]
    by_period = {x["period"]: x for x in rows}
    assert set(by_period) == {D1S[:7], OTHER_MONTH.date().isoformat()[:7]}
    other = by_period[OTHER_MONTH.date().isoformat()[:7]]
    assert other["in_avg"] == pytest.approx(800.0)
    assert other["p95"] == pytest.approx(764.0)  # 两值 [80,800] inclusive 插值
    assert by_period[D1S[:7]]["in_avg"] == pytest.approx(250.0)


def test_traffic_top(client, admin_token, report_data):
    r = client.get(f"/api/reports/traffic?start={D1S}&end={D1S}&top=1",
                   headers=auth(admin_token))
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["interface"] == "GE0/0/2"


def test_traffic_empty_and_bad_params(client, admin_token, report_data):
    r = client.get("/api/reports/traffic?start=2020-01-01&end=2020-01-02",
                   headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["rows"] == []
    assert client.get(f"/api/reports/traffic?start={D1S}&end={D2S}&granularity=week",
                      headers=auth(admin_token)).status_code == 400
    assert client.get(f"/api/reports/traffic?start=bad&end={D2S}",
                      headers=auth(admin_token)).status_code == 400


def test_export_xlsx(client, admin_token, report_data):
    r = client.get(f"/api/reports/availability?start={D1S}&end={D2S}&format=xlsx",
                   headers=auth(admin_token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert len(r.content) > 100 and r.content[:2] == b"PK"  # xlsx 即 zip
    r = client.get(f"/api/reports/traffic?start={D1S}&end={D1S}&format=xlsx",
                   headers=auth(admin_token))
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_reports_require_login(client, report_data):
    assert client.get(f"/api/reports/availability?start={D1S}&end={D2S}").status_code == 401


def test_xlsx_formula_injection_sanitized():
    """Excel 公式注入：字符串以 = + - @ 或制表/换行开头时前缀单引号转义为文本。"""
    from app.api.reports import _sanitize_cell

    assert _sanitize_cell("=SUM(A1)") == "'=SUM(A1)"
    assert _sanitize_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert _sanitize_cell("-1+2") == "'-1+2"
    assert _sanitize_cell("@x") == "'@x"
    assert _sanitize_cell("\t=x") == "'\t=x"
    assert _sanitize_cell("正常设备名") == "正常设备名"
    assert _sanitize_cell("IP 10.0.0.1") == "IP 10.0.0.1"
    assert _sanitize_cell(123) == 123
    assert _sanitize_cell(1.5) == 1.5
    assert _sanitize_cell(None) is None
