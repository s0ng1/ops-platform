"""用户管理增强：改角色 / 禁用启用（PUT /api/users/{id}）及禁用即时生效。"""
from conftest import auth


def _ensure_user(client, admin_token, username, password, role):
    """创建用户；已存在（跨测试文件共享会话库）则返回现有 id。"""
    r = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": role},
        headers=auth(admin_token),
    )
    assert r.status_code in (201, 409), r.text
    r = client.get("/api/users", headers=auth(admin_token))
    user = next(u for u in r.json() if u["username"] == username)
    return user["id"]


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _admin_id(client, admin_token):
    r = client.get("/api/users", headers=auth(admin_token))
    return next(u for u in r.json() if u["username"] == "admin")["id"]


def test_update_role_success_and_audit(client, admin_token):
    uid = _ensure_user(client, admin_token, "m6b_role1", "pass1234", "viewer")
    r = client.put(f"/api/users/{uid}", json={"role": "operator"}, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "operator"
    assert r.json()["disabled"] is False

    r = client.get("/api/audits", params={"action": "user_update"}, headers=auth(admin_token))
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["target"] == "m6b_role1" and "viewer -> operator" in i["detail"] for i in items)


def test_list_users_exposes_disabled(client, admin_token):
    r = client.get("/api/users", headers=auth(admin_token))
    assert r.status_code == 200
    assert all("disabled" in u for u in r.json())


def test_disabled_user_login_rejected(client, admin_token):
    uid = _ensure_user(client, admin_token, "m6b_login1", "pass1234", "viewer")
    r = client.put(f"/api/users/{uid}", json={"disabled": True}, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["disabled"] is True

    r = _login(client, "m6b_login1", "pass1234")
    assert r.status_code == 403, r.text
    assert "禁用" in r.json()["detail"]

    # 审计：禁用动作 + 被禁用户登录失败均有记录
    r = client.get("/api/audits", params={"action": "user_disable"}, headers=auth(admin_token))
    assert any(i["target"] == "m6b_login1" for i in r.json()["items"])

    # 重新启用后恢复登录
    r = client.put(f"/api/users/{uid}", json={"disabled": False}, headers=auth(admin_token))
    assert r.status_code == 200
    r = _login(client, "m6b_login1", "pass1234")
    assert r.status_code == 200, r.text


def test_disabled_user_old_token_invalidated(client, admin_token):
    uid = _ensure_user(client, admin_token, "m6b_token1", "pass1234", "viewer")
    # 先确认处于启用态并能登录（用例可重入）
    client.put(f"/api/users/{uid}", json={"disabled": False}, headers=auth(admin_token))
    r = _login(client, "m6b_token1", "pass1234")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200

    # 禁用后旧 token 立即失效（deps 每次请求查库校验 disabled）
    r = client.put(f"/api/users/{uid}", json={"disabled": True}, headers=auth(admin_token))
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 401

    # 清理：恢复启用，避免影响其他用例
    client.put(f"/api/users/{uid}", json={"disabled": False}, headers=auth(admin_token))


def test_cannot_disable_self(client, admin_token):
    uid = _admin_id(client, admin_token)
    r = client.put(f"/api/users/{uid}", json={"disabled": True}, headers=auth(admin_token))
    assert r.status_code == 400, r.text
    assert "自己" in r.json()["detail"]


def test_cannot_demote_self(client, admin_token):
    uid = _admin_id(client, admin_token)
    r = client.put(f"/api/users/{uid}", json={"role": "viewer"}, headers=auth(admin_token))
    assert r.status_code == 400, r.text


def test_last_active_admin_cannot_be_removed(client, admin_token):
    """两个 active admin 时可禁用其一；剩最后一个 active admin 时不可再禁用/降级。"""
    uid2 = _ensure_user(client, admin_token, "m6b_admin2", "pass1234", "admin")
    # 确保两个 admin 均为启用态
    client.put(f"/api/users/{uid2}", json={"disabled": False}, headers=auth(admin_token))
    r = _login(client, "m6b_admin2", "pass1234")
    assert r.status_code == 200, r.text
    token2 = r.json()["token"]

    admin_uid = _admin_id(client, admin_token)
    # admin2 禁用 admin：还剩 admin2 一个 active admin，允许
    r = client.put(f"/api/users/{admin_uid}", json={"disabled": True}, headers=auth(token2))
    assert r.status_code == 200, r.text

    # 此时 admin2 是唯一 active admin：禁用/降级自己都被拒
    r = client.put(f"/api/users/{uid2}", json={"disabled": True}, headers=auth(token2))
    assert r.status_code == 400, r.text
    r = client.put(f"/api/users/{uid2}", json={"role": "operator"}, headers=auth(token2))
    assert r.status_code == 400, r.text

    # 清理：恢复 admin 启用态
    r = client.put(f"/api/users/{admin_uid}", json={"disabled": False}, headers=auth(token2))
    assert r.status_code == 200, r.text


def test_viewer_cannot_update_user(client, admin_token, viewer_token):
    uid = _ensure_user(client, admin_token, "m6b_viewer1", "pass1234", "viewer")
    r = client.put(f"/api/users/{uid}", json={"role": "operator"}, headers=auth(viewer_token))
    assert r.status_code == 403, r.text
    r = client.put(f"/api/users/{uid}", json={"disabled": True}, headers=auth(viewer_token))
    assert r.status_code == 403, r.text
