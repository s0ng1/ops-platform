"""拓扑 API 测试：连线 CRUD、布局保存、图数据（含流量）、自动发现。"""
import asyncio

from app.core.database import SessionLocal
from app.models import Metric
from app.models.metric import utcnow
from conftest import auth


def _device(client, token, ip, name, type="network"):
    r = client.post("/api/devices", json={"ip": ip, "name": name, "type": type},
                    headers=auth(token))
    if r.status_code == 409:
        # 关键字是模糊匹配（203.0.113.1 会误中 203.0.113.11），须精确过滤
        r = client.get(f"/api/devices?keyword={ip}", headers=auth(token))
        return next(d["id"] for d in r.json() if d["ip"] == ip)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_link_crud_and_graph(client, admin_token):
    d1 = _device(client, admin_token, "203.0.113.1", "核心交换机")
    d2 = _device(client, admin_token, "203.0.113.2", "接入交换机")

    r = client.post("/api/topology/links",
                    json={"src_device_id": d1, "src_port": "GE0/0/1",
                          "dst_device_id": d2, "dst_port": "GE0/0/24"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    link_id = r.json()["id"]

    # 方向颠倒的同一条链路应 409
    r = client.post("/api/topology/links",
                    json={"src_device_id": d2, "src_port": "GE0/0/24",
                          "dst_device_id": d1, "dst_port": "GE0/0/1"},
                    headers=auth(admin_token))
    assert r.status_code == 409

    # 塞流量数据，验证图接口带回流量
    db = SessionLocal()
    db.add(Metric(time=utcnow(), device_id=d1, metric="if_in_bps",
                  labels={"if": "GE0/0/1"}, value=8.5e8))
    db.add(Metric(time=utcnow(), device_id=d1, metric="if_in_util",
                  labels={"if": "GE0/0/1"}, value=85.0))
    db.commit()
    db.close()

    r = client.get("/api/topology/graph", headers=auth(admin_token))
    assert r.status_code == 200
    graph = r.json()
    assert any(n["id"] == d1 for n in graph["nodes"])
    link = next(l for l in graph["links"] if l["id"] == link_id)
    assert link["src_traffic"]["in_bps"] == 850000000.0
    assert link["src_traffic"]["in_util"] == 85.0

    assert client.delete(f"/api/topology/links/{link_id}", headers=auth(admin_token)).status_code == 200


def test_layout_save(client, admin_token):
    d1 = _device(client, admin_token, "203.0.113.1", "核心交换机")
    r = client.put("/api/topology/layout",
                   json={"positions": [{"device_id": d1, "x": 120.5, "y": 300},
                                       {"device_id": 99999, "x": 0, "y": 0}]},
                   headers=auth(admin_token))
    assert r.json()["updated"] == 1
    r = client.get("/api/topology/graph", headers=auth(admin_token))
    node = next(n for n in r.json()["nodes"] if n["id"] == d1)
    assert node["x"] == 120.5 and node["y"] == 300.0


def test_discover_with_mock(client, admin_token, monkeypatch):
    """假 LLDP 数据驱动 /api/topology/discover：自动建链 + 去重 + 未匹配统计。"""
    d1 = _device(client, admin_token, "203.0.113.1", "核心交换机")
    d2 = _device(client, admin_token, "203.0.113.2", "接入交换机")

    # 给两台设备挂 SNMP 凭据
    r = client.post("/api/credentials",
                    json={"name": "拓扑测试SNMP", "kind": "snmp_v2c",
                          "payload": {"community": "public"}},
                    headers=auth(admin_token))
    cid = r.json()["id"] if r.status_code == 201 else client.get(
        "/api/credentials", headers=auth(admin_token)).json()[0]["id"]
    for did, ip, name in ((d1, "203.0.113.1", "核心交换机"), (d2, "203.0.113.2", "接入交换机")):
        client.put(f"/api/devices/{did}",
                   json={"ip": ip, "name": name,
                         "type": "network", "credential_id": cid},
                   headers=auth(admin_token))

    from app.topology import discovery as topo_disc

    async def fake_neighbors(device, payload, fetch_walk=None):
        if device.id == d1:
            return [topo_disc.Neighbor("GE0/0/1", "203.0.113.2", "接入交换机", "GE0/0/24", "lldp"),
                    topo_disc.Neighbor("GE0/0/2", "10.9.9.9", "陌生设备", "GE1", "lldp")]
        return []

    monkeypatch.setattr("app.api.topology.discover_device_neighbors", fake_neighbors)

    r = client.post("/api/topology/discover", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["created"] == 1
    assert len(summary["unmatched"]) == 1

    # 再跑一次：已存在则跳过
    r = client.post("/api/topology/discover", headers=auth(admin_token))
    assert r.json()["created"] == 0
    assert r.json()["skipped"] == 1
