"""操作审计测试：登录成功/失败落审计、设备增删改落审计、查询过滤分页、权限。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from conftest import auth


def _audits(client, token, **params):
    r = client.get("/api/audits", params=params, headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_login_success_and_failure_audited(client, admin_token):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401

    data = _audits(client, admin_token, action="login", username="admin")
    assert data["total"] >= 1
    assert data["items"][0]["ip"]  # TestClient 有 client host

    data = _audits(client, admin_token, action="login_failed")
    assert data["total"] >= 1
    assert any(i["username"] == "admin" for i in data["items"])


def test_device_crud_audited(client, admin_token):
    r = client.post(
        "/api/devices",
        json={"ip": "198.51.100.40", "name": "审计测试机", "type": "network"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get("/api/devices?keyword=198.51.100.40", headers=auth(admin_token))
        did = r.json()[0]["id"]
    else:
        assert r.status_code == 201, r.text
        did = r.json()["id"]
    r = client.put(
        f"/api/devices/{did}",
        json={"ip": "198.51.100.40", "name": "审计测试机2", "type": "network"},
        headers=auth(admin_token),
    )
    assert r.status_code == 200
    assert client.delete(f"/api/devices/{did}", headers=auth(admin_token)).status_code == 200

    creates = _audits(client, admin_token, action="device_create")
    assert any("198.51.100.40" in i["target"] for i in creates["items"])
    updates = _audits(client, admin_token, action="device_update")
    assert any("198.51.100.40" in i["target"] for i in updates["items"])
    deletes = _audits(client, admin_token, action="device_delete")
    assert any("198.51.100.40" in i["target"] for i in deletes["items"])


def test_audits_pagination_and_time_filter(client, admin_token):
    # 造几条新审计记录
    for i in range(3):
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})

    page1 = _audits(client, admin_token, action="login", page=1, page_size=2)
    assert page1["total"] >= 3
    assert len(page1["items"]) == 2
    page2 = _audits(client, admin_token, action="login", page=2, page_size=2)
    assert len(page2["items"]) >= 1
    ids1 = {i["id"] for i in page1["items"]}
    ids2 = {i["id"] for i in page2["items"]}
    assert not ids1 & ids2  # 两页不重叠

    # 时间过滤：未来起点查不到，昨天的起点能查到
    future = (datetime.now() + timedelta(days=1)).isoformat()
    assert _audits(client, admin_token, action="login", start=future)["total"] == 0
    past = (datetime.now() - timedelta(days=1)).isoformat()
    assert _audits(client, admin_token, action="login", start=past)["total"] >= 3


def test_audits_admin_only(client, viewer_token):
    assert client.get("/api/audits", headers=auth(viewer_token)).status_code == 403
    assert client.get("/api/audits").status_code == 401


def test_client_ip_headers_priority():
    """反代下取 X-Forwarded-For 首个，回退 X-Real-IP，再回退直连对端。"""
    from app.core.audit import client_ip

    def req(headers):
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host="10.0.0.5"))

    assert client_ip(req({"x-forwarded-for": "1.2.3.4, 5.6.7.8"})) == "1.2.3.4"
    assert client_ip(req({"x-real-ip": "9.9.9.9"})) == "9.9.9.9"
    assert client_ip(req({})) == "10.0.0.5"
    assert client_ip(None) == ""


def test_credential_and_rule_audited(client, admin_token):
    r = client.post(
        "/api/credentials",
        json={"name": "审计凭据", "kind": "snmp_v2c", "payload": {"community": "public"}},
        headers=auth(admin_token),
    )
    assert r.status_code in (201, 409), r.text
    data = _audits(client, admin_token, action="credential_create")
    assert any(i["target"] == "审计凭据" for i in data["items"])
    # 审计不含密钥明文
    assert "public" not in str(data["items"])
    # 清理：保持凭据表为空，避免干扰后续用例的既有假设
    r = client.get("/api/credentials", headers=auth(admin_token))
    for c in r.json():
        if c["name"] == "审计凭据":
            client.delete(f"/api/credentials/{c['id']}", headers=auth(admin_token))

    body = {"name": "审计规则", "metric": "cpu_usage", "op": ">", "threshold": 90,
            "severity": "info"}
    r = client.post("/api/alert/rules", json=body, headers=auth(admin_token))
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json().get("escalate_minutes") == 0  # 新字段默认 0
    client.delete(f"/api/alert/rules/{rid}", headers=auth(admin_token))
    creates = _audits(client, admin_token, action="alert_rule_create")
    assert any(i["target"] == "审计规则" for i in creates["items"])
    deletes = _audits(client, admin_token, action="alert_rule_delete")
    assert any(i["target"] == "审计规则" for i in deletes["items"])
