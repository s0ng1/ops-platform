"""链路实时流量接口（/api/topology/traffic）与流量查询时间窗测试。"""
from datetime import timedelta

from app.core.database import SessionLocal
from app.models import Metric
from app.models.metric import utcnow
from conftest import auth
from tests.test_topology_api import _device


def test_traffic_endpoint_and_time_window(client, admin_token):
    d1 = _device(client, admin_token, "192.0.2.61", "流量窗-核心")
    d2 = _device(client, admin_token, "192.0.2.62", "流量窗-接入")
    r = client.post("/api/topology/links",
                    json={"src_device_id": d1, "src_port": "GE0/0/1",
                          "dst_device_id": d2, "dst_port": "GE0/0/24"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    link_id = r.json()["id"]

    now = utcnow()
    db = SessionLocal()
    # 近 10 分钟内：应返回
    db.add(Metric(time=now, device_id=d1, metric="if_in_bps",
                  labels={"if": "GE0/0/1"}, value=1.2e9))
    db.add(Metric(time=now, device_id=d1, metric="if_out_util",
                  labels={"if": "GE0/0/1"}, value=66.0))
    # 10 分钟前的旧数据：时间窗外，不应作为最新值返回
    db.add(Metric(time=now - timedelta(minutes=30), device_id=d1, metric="if_in_bps",
                  labels={"if": "GE0/0/1"}, value=9.9e9))
    db.commit()
    db.close()

    r = client.get("/api/topology/traffic", headers=auth(admin_token))
    assert r.status_code == 200
    payload = r.json()
    assert "nodes" not in payload  # 轻量接口不含节点
    link = next(l for l in payload["links"] if l["id"] == link_id)
    # 取到的是时间窗内的新值而不是 30 分钟前的旧值
    assert link["src_traffic"]["in_bps"] == 1200000000.0
    assert link["src_traffic"]["out_util"] == 66.0
    assert link["dst_traffic"]["in_bps"] is None

    # group 过滤：不在组内的链路不返回
    r = client.get("/api/topology/traffic?group=不存在的分组", headers=auth(admin_token))
    assert r.status_code == 200
    assert r.json()["links"] == []
