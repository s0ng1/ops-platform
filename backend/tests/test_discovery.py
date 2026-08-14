import time

from conftest import auth


def test_scan_job_lifecycle_and_import(client, admin_token):
    r = client.post(
        "/api/discovery/scan",
        json={"ranges": "127.0.0.1"},
        headers=auth(admin_token),
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    # 轮询等待后台扫描完成（本机 ping 应很快）
    job = None
    for _ in range(60):
        r = client.get(f"/api/discovery/jobs/{job_id}", headers=auth(admin_token))
        job = r.json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert job["status"] == "done", job
    assert len(job["results"]) == 1
    assert job["results"][0]["ip"] == "127.0.0.1"

    r = client.post(
        "/api/discovery/import",
        json={"ips": ["127.0.0.1"], "type": "server_linux", "group_name": "本机"},
        headers=auth(admin_token),
    )
    assert r.status_code == 201
    # 重复导入应跳过
    r = client.post(
        "/api/discovery/import",
        json={"ips": ["127.0.0.1"], "type": "server_linux"},
        headers=auth(admin_token),
    )
    assert r.json()["created"] == 0


def test_scan_invalid_ranges(client, admin_token):
    r = client.post(
        "/api/discovery/scan",
        json={"ranges": "abc-def"},
        headers=auth(admin_token),
    )
    assert r.status_code == 400


def test_import_rejects_invalid_ip_and_over_limit(client, admin_token):
    """批量入库：非法 IP 400 并列出行；超过上限 400。"""
    r = client.post(
        "/api/discovery/import",
        json={"ips": ["192.0.2.1", "not-an-ip"], "type": "other"},
        headers=auth(admin_token),
    )
    assert r.status_code == 400
    assert "非法 IP" in r.json()["detail"]

    too_many = [f"10.99.{i // 256}.{i % 256}" for i in range(5001)]
    r = client.post(
        "/api/discovery/import",
        json={"ips": too_many, "type": "other"},
        headers=auth(admin_token),
    )
    assert r.status_code == 400
    assert "最多导入" in r.json()["detail"]
