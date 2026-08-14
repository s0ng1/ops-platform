"""第 8 期 M4：子网/机房分组子拓扑测试。

分组清单、graph?group= 过滤、分组布局保存与回退、设备表单 group_name 写入与校验。
IP 段：192.0.2.6x（本文件专用）；组名带 M4 前缀避免与其他用例的分组互相干扰。
"""
from conftest import auth

GROUP_A = "M4机房A"
GROUP_B = "M4机房B"


def _device(client, token, ip, name, type="network", **kw):
    r = client.post("/api/devices", json={"ip": ip, "name": name, "type": type, **kw},
                    headers=auth(token))
    if r.status_code == 409:
        r = client.get(f"/api/devices?keyword={ip}", headers=auth(token))
        return next(d["id"] for d in r.json() if d["ip"] == ip and d["type"] == type)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _link(client, token, src, dst, sp="GE0/0/1", dp="GE0/0/2"):
    r = client.post("/api/topology/links",
                    json={"src_device_id": src, "src_port": sp,
                          "dst_device_id": dst, "dst_port": dp},
                    headers=auth(token))
    assert r.status_code in (201, 409), r.text
    return r


def _topo_devices(client, admin_token):
    """造两台 A 组、一台 B 组、一台未分组的拓扑设备，返回 id 四元组。"""
    a1 = _device(client, admin_token, "192.0.2.61", "M4-A1", group_name=GROUP_A)
    a2 = _device(client, admin_token, "192.0.2.62", "M4-A2", type="security", group_name=GROUP_A)
    b1 = _device(client, admin_token, "192.0.2.63", "M4-B1", group_name=GROUP_B)
    free = _device(client, admin_token, "192.0.2.64", "M4-未分组")
    return a1, a2, b1, free


def test_groups_list(client, admin_token):
    """groups 清单：只统计 network/security 设备，含设备数；服务器分组不计入。"""
    a1, a2, b1, free = _topo_devices(client, admin_token)
    # 服务器设备也带分组名，但不应出现在拓扑分组清单里
    _device(client, admin_token, "192.0.2.65", "M4-服务器", type="server_linux", group_name=GROUP_A)

    r = client.get("/api/topology/groups", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    groups = {g["name"]: g["count"] for g in r.json()}
    assert groups[GROUP_A] == 2  # a1(network) + a2(security)，服务器不算
    assert groups[GROUP_B] == 1


def test_graph_group_filter(client, admin_token):
    """graph?group=：只出组内节点 + 两端都在组内的链路；跨界链路剔除但全图保留。"""
    a1, a2, b1, free = _topo_devices(client, admin_token)
    _link(client, admin_token, a1, a2)          # 组内链路
    _link(client, admin_token, a1, b1)          # 跨界链路
    _link(client, admin_token, b1, free)        # 组外链路

    # 分组视图：只有 A 组两台 + 组内一条链路
    r = client.get(f"/api/topology/graph?group={GROUP_A}", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    g = r.json()
    node_ids = {n["id"] for n in g["nodes"]}
    assert node_ids == {a1, a2}
    endpoints = {(l["src_device_id"], l["dst_device_id"]) for l in g["links"]}
    assert endpoints == {(a1, a2)}

    # 全图（不带 group）行为不变：四台都在，跨界/组外链路保留
    r = client.get("/api/topology/graph", headers=auth(admin_token))
    g = r.json()
    node_ids = {n["id"] for n in g["nodes"]}
    assert {a1, a2, b1, free} <= node_ids
    endpoints = {(l["src_device_id"], l["dst_device_id"]) for l in g["links"]}
    assert {(a1, a2), (a1, b1), (b1, free)} <= endpoints


def test_group_layout_save_and_fallback(client, admin_token):
    """分组布局：有记录用记录、无记录回退全图坐标、全图布局不受分组布局影响。"""
    a1, a2, b1, free = _topo_devices(client, admin_token)

    # 全图布局（不带 group）：写 devices.pos_x/pos_y
    r = client.put("/api/topology/layout",
                   json={"positions": [{"device_id": a1, "x": 100, "y": 100},
                                       {"device_id": a2, "x": 300, "y": 100}]},
                   headers=auth(admin_token))
    assert r.json()["updated"] == 2

    # 分组布局（带 group）：只给 a1 摆位
    r = client.put("/api/topology/layout",
                   json={"group": GROUP_A,
                         "positions": [{"device_id": a1, "x": 10, "y": 20},
                                       {"device_id": 99999, "x": 0, "y": 0}]},
                   headers=auth(admin_token))
    assert r.json()["updated"] == 1

    # 分组视图：a1 用分组布局记录，a2 无记录回退全图坐标
    g = client.get(f"/api/topology/graph?group={GROUP_A}", headers=auth(admin_token)).json()
    n1 = next(n for n in g["nodes"] if n["id"] == a1)
    n2 = next(n for n in g["nodes"] if n["id"] == a2)
    assert (n1["x"], n1["y"]) == (10, 20)
    assert (n2["x"], n2["y"]) == (300, 100)

    # 全图布局不受分组布局影响：a1 仍是全图坐标
    g = client.get("/api/topology/graph", headers=auth(admin_token)).json()
    n1 = next(n for n in g["nodes"] if n["id"] == a1)
    assert (n1["x"], n1["y"]) == (100, 100)

    # 更新分组布局（同 device+group 唯一，覆盖而不是新增）
    r = client.put("/api/topology/layout",
                   json={"group": GROUP_A, "positions": [{"device_id": a1, "x": 50, "y": 60}]},
                   headers=auth(admin_token))
    assert r.json()["updated"] == 1
    g = client.get(f"/api/topology/graph?group={GROUP_A}", headers=auth(admin_token)).json()
    n1 = next(n for n in g["nodes"] if n["id"] == a1)
    assert (n1["x"], n1["y"]) == (50, 60)
    # 全图依旧不变
    g = client.get("/api/topology/graph", headers=auth(admin_token)).json()
    n1 = next(n for n in g["nodes"] if n["id"] == a1)
    assert (n1["x"], n1["y"]) == (100, 100)


def test_device_group_name_write_and_validate(client, admin_token):
    """设备表单 group_name：创建/更新写入、超长校验。"""
    r = client.post("/api/devices",
                    json={"ip": "192.0.2.66", "name": "M4-表单", "type": "network",
                          "group_name": GROUP_A},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert r.json()["group_name"] == GROUP_A

    # 更新分组 / 清空分组
    r = client.put(f"/api/devices/{did}",
                   json={"ip": "192.0.2.66", "name": "M4-表单", "type": "network",
                         "group_name": GROUP_B},
                   headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["group_name"] == GROUP_B
    r = client.put(f"/api/devices/{did}",
                   json={"ip": "192.0.2.66", "name": "M4-表单", "type": "network",
                         "group_name": ""},
                   headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["group_name"] == ""

    # 超长（>128）校验失败
    r = client.put(f"/api/devices/{did}",
                   json={"ip": "192.0.2.66", "name": "M4-表单", "type": "network",
                         "group_name": "x" * 129},
                   headers=auth(admin_token))
    assert r.status_code == 422
