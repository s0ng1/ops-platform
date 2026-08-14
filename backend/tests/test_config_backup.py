"""配置备份：采集器（mock run_commands）+ 版本去重 + 变更事件 + diff/fetch API + 权限。
注意：全测试套件共享一个会话级库——设备 IP 用 198.51.100.x 段避开其他用例，
凭据用完即删（test_credentials 有「列表首条」断言，不能被本文件插队）。
"""
import asyncio
from types import SimpleNamespace

from conftest import auth

from app.collectors import config_backup as cb
from app.core.database import SessionLocal
from app.models import Device

CONFIG_V1 = "version 7.1\nsysname SW1\ninterface GigabitEthernet1/0/1\n port link-mode route\n"
CONFIG_V2 = "version 7.1\nsysname SW1\ninterface GigabitEthernet1/0/1\n port link-mode bridge\n"


class FakeDevice:
    id = 1
    ip = "198.51.100.99"
    sys_object_id = ""


def _make_device(client, token, ip, credential_id=None, ssh_credential_id=None):
    """建一台 network 设备，返回设备 id。"""
    r = client.post(
        "/api/devices",
        json={
            "ip": ip, "name": f"sw-{ip}", "type": "network",
            "credential_id": credential_id, "ssh_credential_id": ssh_credential_id,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_ssh_credential(client, token, name):
    r = client.post(
        "/api/credentials",
        json={"name": name, "kind": "ssh", "payload": {"username": "backup", "password": "secret"}},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _db_device(device_id):
    db = SessionLocal()
    try:
        return db.get(Device, device_id)
    finally:
        db.close()


def _fake_runner(text):
    async def fake_run(host, payload, commands):
        assert commands == [cb.DEFAULT_COMMAND]  # 测试设备无指纹，走 H3C 兜底命令
        return [text]
    return fake_run


def test_select_command_by_vendor():
    d = FakeDevice()
    d.sys_object_id = "1.3.6.1.4.1.9.1.1"      # Cisco
    assert cb.select_command(d) == "show running-config"
    d.sys_object_id = "1.3.6.1.4.1.2011.2.3"   # 华为
    assert cb.select_command(d) == "display current-configuration"
    d.sys_object_id = "1.3.6.1.4.1.25506.1.639"  # H3C
    assert cb.select_command(d) == "display current-configuration"
    d.sys_object_id = ""                        # 未识别按 H3C 兜底
    assert cb.select_command(d) == cb.DEFAULT_COMMAND


def test_truncate_protection():
    text = "x" * (cb.MAX_CONFIG_BYTES + 100)
    assert len(cb._truncate(text).encode("utf-8")) == cb.MAX_CONFIG_BYTES


def test_fetch_baseline_then_dedup(client, admin_token):
    did = _make_device(client, admin_token, "198.51.100.1")
    device = _db_device(did)

    # 首次拉取：baseline 入库（采集器 payload 由调用方传入，无需真凭据）
    r = asyncio.run(cb.fetch_config(device, {}, run_commands=_fake_runner(CONFIG_V1)))
    assert r["status"] == "baseline"
    # 同内容再拉：不新增版本
    r = asyncio.run(cb.fetch_config(device, {}, run_commands=_fake_runner(CONFIG_V1)))
    assert r["status"] == "same"

    resp = client.get(f"/api/devices/{did}/config-backups", headers=auth(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert len(item["content_hash"]) == 64 and item["size"] > 0

    # 单版本全文
    resp = client.get(f"/api/devices/{did}/config-backups/{item['id']}", headers=auth(admin_token))
    assert resp.status_code == 200
    assert resp.json()["content"] == CONFIG_V1


def test_ssh_failure_silent(client, admin_token):
    did = _make_device(client, admin_token, "198.51.100.2")
    device = _db_device(did)

    async def bad_run(host, payload, commands):
        raise OSError("connection refused")

    r = asyncio.run(cb.fetch_config(device, {}, run_commands=bad_run))
    assert r["status"] == "failed"
    resp = client.get(f"/api/devices/{did}/config-backups", headers=auth(admin_token))
    assert resp.json()["total"] == 0


def test_change_fires_event_via_manual_fetch(client, admin_token, monkeypatch):
    cred_id = _make_ssh_credential(client, admin_token, "ssh-198.51.100.3")
    try:
        did = _make_device(client, admin_token, "198.51.100.3", ssh_credential_id=cred_id)
        state = {"text": CONFIG_V1}
        real_fetch = cb.fetch_config

        async def fake_run(host, payload, commands):
            return [state["text"]]

        # API 内部走 cb.fetch_config 默认注入点，替换为带假输出的包装
        async def wrapped(device, payload, run_commands=None):
            return await real_fetch(device, payload, run_commands=fake_run)

        monkeypatch.setattr(cb, "fetch_config", wrapped)

        r = client.post(f"/api/devices/{did}/config-backups/fetch", headers=auth(admin_token))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "baseline"

        state["text"] = CONFIG_V2
        r = client.post(f"/api/devices/{did}/config-backups/fetch", headers=auth(admin_token))
        assert r.json()["status"] == "changed"

        # 变更触发 info 级「配置变更」事件
        resp = client.get(f"/api/alert/events?device_id={did}", headers=auth(admin_token))
        events = [e for e in resp.json() if e["rule_name"] == "配置变更"]
        assert len(events) == 1
        assert events[0]["severity"] == "info"
        assert events[0]["status"] == "firing"

        # 内容不再变化时下一轮自动恢复（config_changed=0 走引擎恢复语义）
        r = client.post(f"/api/devices/{did}/config-backups/fetch", headers=auth(admin_token))
        assert r.json()["status"] == "same"
        resp = client.get(f"/api/alert/events?device_id={did}", headers=auth(admin_token))
        event = next(e for e in resp.json() if e["rule_name"] == "配置变更")
        assert event["status"] == "resolved"

        # 两个版本入库
        resp = client.get(f"/api/devices/{did}/config-backups", headers=auth(admin_token))
        assert resp.json()["total"] == 2
    finally:
        # 凭据用完即删：test_credentials 断言列表首条是本用例之后建的「默认SNMP」
        client.delete(f"/api/credentials/{cred_id}", headers=auth(admin_token))


def test_diff_api(client, admin_token):
    did = _make_device(client, admin_token, "198.51.100.4")
    device = _db_device(did)
    run = asyncio.run
    run(cb.fetch_config(device, {}, run_commands=_fake_runner(CONFIG_V1)))
    run(cb.fetch_config(device, {}, run_commands=_fake_runner(CONFIG_V2)))

    resp = client.get(f"/api/devices/{did}/config-backups", headers=auth(admin_token))
    items = resp.json()["items"]  # 倒序：items[0] 是新版本
    new_id, old_id = items[0]["id"], items[1]["id"]

    resp = client.get(
        f"/api/devices/{did}/config-backups/diff?from={old_id}&to={new_id}",
        headers=auth(admin_token),
    )
    assert resp.status_code == 200
    text = resp.text
    assert "- port link-mode route" in text
    assert "+ port link-mode bridge" in text

    # 版本不属于该设备 → 404
    resp = client.get(
        f"/api/devices/{did}/config-backups/diff?from={old_id}&to=999999",
        headers=auth(admin_token),
    )
    assert resp.status_code == 404


def test_fetch_requires_ssh_credential(client, admin_token):
    did = _make_device(client, admin_token, "198.51.100.5")
    r = client.post(f"/api/devices/{did}/config-backups/fetch", headers=auth(admin_token))
    assert r.status_code == 400
    assert "备份用 SSH 凭据" in r.json()["detail"]


def test_fetch_main_slot_ssh_credential_not_used(client, admin_token):
    """SSH 凭据挂在主槽不算数：手动 fetch 与调度 scope 都只认辅槽。"""
    cred_id = _make_ssh_credential(client, admin_token, "ssh-198.51.100.7")
    try:
        did = _make_device(client, admin_token, "198.51.100.7", credential_id=cred_id)
        r = client.post(f"/api/devices/{did}/config-backups/fetch", headers=auth(admin_token))
        assert r.status_code == 400
        assert "备份用 SSH 凭据" in r.json()["detail"]
    finally:
        client.delete(f"/api/credentials/{cred_id}", headers=auth(admin_token))


def test_ssh_credential_id_validation(client, admin_token):
    """辅槽凭据：非 ssh 类型 422，不存在 400，ssh 类型正常。"""
    r = client.post(
        "/api/credentials",
        json={"name": "snmp-198.51.100.8", "kind": "snmp_v2c", "payload": {"community": "public"}},
        headers=auth(admin_token),
    )
    assert r.status_code == 201, r.text
    snmp_cred_id = r.json()["id"]
    ssh_cred_id = _make_ssh_credential(client, admin_token, "ssh-198.51.100.8")
    try:
        # snmp 凭据挂辅槽 → 422
        r = client.post(
            "/api/devices",
            json={"ip": "198.51.100.8", "type": "network", "ssh_credential_id": snmp_cred_id},
            headers=auth(admin_token),
        )
        assert r.status_code == 422
        # 不存在的凭据 → 400（照主槽「凭据不存在」风格）
        r = client.post(
            "/api/devices",
            json={"ip": "198.51.100.8", "type": "network", "ssh_credential_id": 999999},
            headers=auth(admin_token),
        )
        assert r.status_code == 400
        # ssh 凭据挂辅槽 → 201，主槽可同时挂 SNMP 凭据
        did = _make_device(
            client, admin_token, "198.51.100.8",
            credential_id=snmp_cred_id, ssh_credential_id=ssh_cred_id,
        )
        resp = client.get(f"/api/devices/{did}", headers=auth(admin_token))
        assert resp.json()["ssh_credential_id"] == ssh_cred_id
    finally:
        client.delete(f"/api/credentials/{snmp_cred_id}", headers=auth(admin_token))
        client.delete(f"/api/credentials/{ssh_cred_id}", headers=auth(admin_token))


def test_fetch_forbidden_for_viewer(client, admin_token, viewer_token):
    did = _make_device(client, admin_token, "198.51.100.6")
    r = client.post(f"/api/devices/{did}/config-backups/fetch", headers=auth(viewer_token))
    assert r.status_code == 403
    # 只读接口 viewer 可看
    r = client.get(f"/api/devices/{did}/config-backups", headers=auth(viewer_token))
    assert r.status_code == 200


def test_viewer_cannot_read_full_config_or_diff(client, admin_token, viewer_token):
    """配置全文与 diff 属敏感内容，viewer 读 403；列表（hash/size 元数据）可看。"""
    did = _make_device(client, admin_token, "198.51.100.11")
    device = _db_device(did)
    asyncio.run(cb.fetch_config(device, {}, run_commands=_fake_runner(CONFIG_V1)))
    items = client.get(f"/api/devices/{did}/config-backups", headers=auth(admin_token)).json()["items"]
    bid = items[0]["id"]

    r = client.get(f"/api/devices/{did}/config-backups/{bid}", headers=auth(viewer_token))
    assert r.status_code == 403
    r = client.get(f"/api/devices/{did}/config-backups/diff?from={bid}&to={bid}",
                   headers=auth(viewer_token))
    assert r.status_code == 403
    r = client.get(f"/api/devices/{did}/config-backups", headers=auth(viewer_token))
    assert r.status_code == 200


def test_scheduler_task_registered():
    from app.scheduler.scheduler import TASKS

    task = next(t for t in TASKS if t.name == "config_backup")
    assert task.interval == 21600

    device = SimpleNamespace(monitor_enabled=True, type="network", ssh_credential_id=1)
    assert task.applies_to(device)
    device.type = "server_linux"  # 服务器不在配置备份范围
    assert not task.applies_to(device)
    device.type = "network"
    device.ssh_credential_id = None  # 未挂辅槽备份凭据不适用
    assert not task.applies_to(device)
    # SSH 凭据挂在主槽不触发备份（scope 只认辅槽）
    device.credential = SimpleNamespace(kind="ssh")
    assert not task.applies_to(device)
