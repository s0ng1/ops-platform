from conftest import auth


def test_credential_roundtrip_and_masking(client, admin_token):
    r = client.post(
        "/api/credentials",
        json={
            "name": "默认SNMP",
            "kind": "snmp_v2c",
            "payload": {"community": "public", "port": 161},
        },
        headers=auth(admin_token),
    )
    assert r.status_code == 201, r.text
    cred_id = r.json()["id"]

    # 列表/详情不回显明文
    r = client.get("/api/credentials", headers=auth(admin_token))
    body = r.text
    assert "public" not in body
    assert r.json()[0]["name"] == "默认SNMP"

    # 设备可绑定凭据
    r = client.post(
        "/api/devices",
        json={"ip": "192.0.2.40", "credential_id": cred_id},
        headers=auth(admin_token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["credential_name"] == "默认SNMP"

    # 解密封装正确性（直接用模型验证加解密往返）
    from app.core.database import SessionLocal
    from app.models import Credential

    db = SessionLocal()
    cred = db.get(Credential, cred_id)
    assert cred.get_payload()["community"] == "public"
    db.close()


def test_credential_duplicate_name(client, admin_token):
    body = {"name": "重复名", "kind": "ssh", "payload": {"username": "a", "password": "b"}}
    assert client.post("/api/credentials", json=body, headers=auth(admin_token)).status_code == 201
    assert client.post("/api/credentials", json=body, headers=auth(admin_token)).status_code == 409
