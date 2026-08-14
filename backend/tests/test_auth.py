from conftest import auth

from app.core import ratelimit


def test_login_ok(client, admin_token):
    assert admin_token


def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_me(client, admin_token):
    r = client.get("/api/auth/me", headers=auth(admin_token))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_unauthorized(client):
    assert client.get("/api/devices").status_code == 401


def test_viewer_cannot_create_device(client, viewer_token):
    r = client.post(
        "/api/devices",
        json={"ip": "10.99.0.1"},
        headers=auth(viewer_token),
    )
    assert r.status_code == 403


def test_viewer_cannot_manage_users(client, viewer_token):
    r = client.get("/api/users", headers=auth(viewer_token))
    assert r.status_code == 403


def test_login_rate_limit_after_repeated_failures(client):
    """连续失败超阈值后返回 429；用一次性用户名并清理，避免污染其它用例。"""
    username = "ratelimit_probe"
    for _ in range(ratelimit.USER_MAX_FAILS):
        r = client.post("/api/auth/login", json={"username": username, "password": "wrong1"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": username, "password": "wrong1"})
    assert r.status_code == 429, r.text
    # 清理该用户名维度的失败计数与锁（ip 维度仅 5 次未达阈值，无需清）
    ratelimit.note_success("testclient", username)


def test_create_user_rejects_weak_password(client, admin_token):
    """密码过短 / 纯数字（无字母）均 422。"""
    r = client.post("/api/users", json={"username": "weakpw1", "password": "short",
                                        "role": "viewer"}, headers=auth(admin_token))
    assert r.status_code == 422, r.text
    r = client.post("/api/users", json={"username": "weakpw2", "password": "12345678",
                                        "role": "viewer"}, headers=auth(admin_token))
    assert r.status_code == 422, r.text


def test_change_password_rejects_weak(client, admin_token):
    """改密同样校验强度；422 在请求体校验阶段触发，不落库、不影响 admin 口令。"""
    r = client.post("/api/auth/change-password",
                    json={"old_password": "admin123", "new_password": "12345678"},
                    headers=auth(admin_token))
    assert r.status_code == 422, r.text
