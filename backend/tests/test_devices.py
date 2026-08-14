from conftest import auth


def _create_device(client, token, ip="192.0.2.10", **kw):
    body = {"ip": ip, "name": "测试交换机", "type": "network", **kw}
    return client.post("/api/devices", json=body, headers=auth(token))


def test_device_crud(client, admin_token):
    r = _create_device(client, admin_token)
    assert r.status_code == 201, r.text
    dev = r.json()
    assert dev["status"] == "unknown"

    r = client.get("/api/devices", headers=auth(admin_token))
    assert any(d["ip"] == "192.0.2.10" for d in r.json())

    r = client.put(
        f"/api/devices/{dev['id']}",
        json={"ip": "192.0.2.10", "name": "改名", "type": "security"},
        headers=auth(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "改名"

    r = client.delete(f"/api/devices/{dev['id']}", headers=auth(admin_token))
    assert r.status_code == 200
    assert client.get(f"/api/devices/{dev['id']}", headers=auth(admin_token)).status_code == 404


def test_duplicate_ip_rejected(client, admin_token):
    assert _create_device(client, admin_token, ip="192.0.2.20").status_code == 201
    assert _create_device(client, admin_token, ip="192.0.2.20").status_code == 409


def test_invalid_ip_rejected(client, admin_token):
    r = client.post("/api/devices", json={"ip": "not-an-ip"}, headers=auth(admin_token))
    assert r.status_code == 400


def test_device_list_filter(client, admin_token):
    _create_device(client, admin_token, ip="192.0.2.30", group_name="核心机房")
    r = client.get("/api/devices?keyword=核心", headers=auth(admin_token))
    # keyword 匹配 name/ip，这里按 ip 过滤更稳
    r = client.get("/api/devices?keyword=192.0.2.30", headers=auth(admin_token))
    assert len(r.json()) == 1
    r = client.get("/api/devices?type=network", headers=auth(admin_token))
    assert all(d["type"] == "network" for d in r.json())


def test_overview(client, admin_token):
    r = client.get("/api/monitor/overview", headers=auth(admin_token))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert data["total"] == data["online"] + data["offline"] + data["unknown"]


def _create_redis_device(client, token, ip="192.0.2.95", password="secretpw"):
    r = client.post(
        "/api/devices",
        json={"ip": ip, "name": f"redis-{ip}", "type": "application",
              "probe_config": {"probe_kind": "redis", "port": 6379, "password": password}},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_redis_password_redacted(client, admin_token):
    """Redis 拨测口令读接口脱敏为星号哨兵（列表与详情均不泄露明文）。"""
    dev = _create_redis_device(client, admin_token)
    assert dev["probe_config"]["password"] == "******"
    r = client.get("/api/devices?type=application", headers=auth(admin_token))
    mine = next(d for d in r.json() if d["ip"] == "192.0.2.95")
    assert mine["probe_config"]["password"] == "******"
    r = client.get(f"/api/devices/{dev['id']}", headers=auth(admin_token))
    assert r.json()["probe_config"]["password"] == "******"


def test_redis_password_preserved_on_sentinel_edit(client, admin_token):
    """编辑时提交星号哨兵=不修改，底层保留原口令；空串=清除。"""
    from app.core.database import SessionLocal
    from app.models import Device

    ip = "192.0.2.96"
    dev = _create_redis_device(client, admin_token, ip=ip)
    # 星号哨兵：不修改
    r = client.put(
        f"/api/devices/{dev['id']}",
        json={"ip": ip, "name": f"redis-{ip}", "type": "application",
              "probe_config": {"probe_kind": "redis", "port": 6379, "password": "******"}},
        headers=auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["probe_config"]["password"] == "******"
    db = SessionLocal()
    try:
        assert db.get(Device, dev["id"]).probe_config["password"] == "secretpw"
    finally:
        db.close()
    # 空串：清除口令
    r = client.put(
        f"/api/devices/{dev['id']}",
        json={"ip": ip, "name": f"redis-{ip}", "type": "application",
              "probe_config": {"probe_kind": "redis", "port": 6379, "password": ""}},
        headers=auth(admin_token),
    )
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.get(Device, dev["id"]).probe_config["password"] == ""
    finally:
        db.close()
