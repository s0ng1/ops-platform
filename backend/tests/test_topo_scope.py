"""拓扑范围过滤与设备多类型过滤测试。"""
from app.core.database import SessionLocal
from app.models import TopoLink
from conftest import auth


def _device(client, token, ip, name, type):
    r = client.post("/api/devices", json={"ip": ip, "name": name, "type": type},
                    headers=auth(token))
    if r.status_code == 409:
        # 关键字是模糊匹配，须精确过滤
        r = client.get(f"/api/devices?keyword={ip}", headers=auth(token))
        return next(d["id"] for d in r.json() if d["ip"] == ip)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_graph_only_network_and_security(client, admin_token):
    net = _device(client, admin_token, "203.0.113.11", "图过滤-网络", "network")
    sec = _device(client, admin_token, "203.0.113.12", "图过滤-防火墙", "security")
    host = _device(client, admin_token, "203.0.113.13", "图过滤-主机", "server_linux")
    dbdev = _device(client, admin_token, "203.0.113.14", "图过滤-数据库", "database")

    # 三条链路：网-安（应出现）、网-主机（不应出现）、主机-数据库（不应出现）
    db = SessionLocal()
    db.query(TopoLink).delete()
    db.add_all([
        TopoLink(src_device_id=net, src_port="GE1", dst_device_id=sec, dst_port="GE1", source="manual"),
        TopoLink(src_device_id=net, src_port="GE2", dst_device_id=host, dst_port="eth0", source="manual"),
        TopoLink(src_device_id=host, src_port="eth0", dst_device_id=dbdev, dst_port="3306", source="manual"),
    ])
    db.commit()
    db.close()

    r = client.get("/api/topology/graph", headers=auth(admin_token))
    graph = r.json()
    node_ids = {n["id"] for n in graph["nodes"]}
    assert net in node_ids and sec in node_ids
    assert host not in node_ids and dbdev not in node_ids
    assert len(graph["links"]) == 1
    link = graph["links"][0]
    assert {link["src_device_id"], link["dst_device_id"]} == {net, sec}


def test_devices_types_multi_filter(client, admin_token):
    r = client.get("/api/devices?types=network,security", headers=auth(admin_token))
    assert r.status_code == 200
    assert all(d["type"] in ("network", "security") for d in r.json())
    r2 = client.get("/api/devices?types=server_linux,database", headers=auth(admin_token))
    assert all(d["type"] in ("server_linux", "database") for d in r2.json())
    # 与原 type 单选并存不冲突
    r3 = client.get("/api/devices?type=network", headers=auth(admin_token))
    assert all(d["type"] == "network" for d in r3.json())
