"""告警规则模板：CRUD、内置模板播种（数量与内置规则对应）、instantiate 幂等、
builtin 删改被拒、viewer 无权、字段校验（含 baseline_dev）。
注意：测试套件共享会话级库，自建模板/规则一律用「模板测试-」前缀避免撞名。
"""
from conftest import auth

from app.core.database import SessionLocal
from app.main import _BUILTIN_RULES, _init_db
from app.models import RuleTemplate


def _templates():
    db = SessionLocal()
    rows = db.query(RuleTemplate).all()
    # 取出快照后关会话，避免 DetachedInstance
    snap = [
        {c: getattr(r, c) for c in
         ("id", "name", "description", "metric", "op", "threshold", "duration_cycles",
          "severity", "device_type", "group_name", "device_id", "labels_filter", "builtin")}
        for r in rows
    ]
    db.close()
    return snap


def test_builtin_templates_seeded(client, admin_token):
    """内置模板按内置规则一一播种：数量、名称、关键字段对应，builtin=True。"""
    r = client.get("/api/alert/templates", headers=auth(admin_token))
    assert r.status_code == 200
    builtin = [t for t in r.json() if t["builtin"]]
    assert len(builtin) == len(_BUILTIN_RULES)
    by_name = {t["name"]: t for t in builtin}
    for rule in _BUILTIN_RULES:
        t = by_name.get(rule["name"])
        assert t is not None, f"缺少内置模板 {rule['name']}"
        assert (t["metric"], t["op"], t["threshold"], t["duration_cycles"],
                t["severity"], t["device_type"]) == \
            (rule["metric"], rule["op"], rule["threshold"], rule["duration_cycles"],
             rule["severity"], rule.get("device_type", "")), f"模板字段不符 {rule['name']}"
    # labels_filter 也要镜像（如 日志事件-致命）
    assert by_name["日志事件-致命"]["labels_filter"] == {"severity": "critical"}


def test_seed_templates_idempotent(client):
    """再跑 _init_db：内置模板数量不变（按名补种，只增不改）。"""
    n1 = len([t for t in _templates() if t["builtin"]])
    _init_db()
    n2 = len([t for t in _templates() if t["builtin"]])
    assert n1 == n2 == len(_BUILTIN_RULES)


def test_template_crud(client, admin_token):
    body = {
        "name": "模板测试-内存过高", "description": "测试用模板",
        "metric": "mem_usage", "op": ">", "threshold": 90,
        "duration_cycles": 2, "severity": "major", "device_type": "server_linux",
        "labels_filter": {"if": "eth0"},
    }
    r = client.post("/api/alert/templates", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert tpl["builtin"] is False
    assert tpl["labels_filter"] == {"if": "eth0"}
    tid = tpl["id"]

    # 同名冲突 409
    r = client.post("/api/alert/templates", json=body, headers=auth(admin_token))
    assert r.status_code == 409

    # 更新
    r = client.put(f"/api/alert/templates/{tid}", json={**body, "threshold": 95},
                   headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["threshold"] == 95.0

    # 改名成已存在的内置模板名 → 409
    r = client.put(f"/api/alert/templates/{tid}", json={**body, "name": "设备离线"},
                   headers=auth(admin_token))
    assert r.status_code == 409

    # 删除
    assert client.delete(f"/api/alert/templates/{tid}", headers=auth(admin_token)).status_code == 200
    assert client.delete(f"/api/alert/templates/{tid}", headers=auth(admin_token)).status_code == 404


def test_builtin_template_immutable(client, admin_token):
    """builtin=true 的模板禁止删改（服务端拒绝，比内置规则更严）。"""
    builtin = next(t for t in _templates() if t["builtin"])
    tid = builtin["id"]
    body = {
        "name": builtin["name"], "metric": builtin["metric"], "op": builtin["op"],
        "threshold": 1, "severity": builtin["severity"],
    }
    r = client.put(f"/api/alert/templates/{tid}", json=body, headers=auth(admin_token))
    assert r.status_code == 400
    r = client.delete(f"/api/alert/templates/{tid}", headers=auth(admin_token))
    assert r.status_code == 400


def test_instantiate_idempotent(client, admin_token):
    """instantiate：首次新建、二次全 skipped（同名跳过幂等）；device_type 可覆盖。"""
    body = {
        "name": "模板测试-CPU过高", "metric": "cpu_usage", "op": ">", "threshold": 88,
        "duration_cycles": 3, "severity": "major",
    }
    r = client.post("/api/alert/templates", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    # 首次：created
    r = client.post("/api/alert/templates/instantiate",
                    json={"template_ids": [tid], "device_type": "server_windows"},
                    headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json() == {"created": ["模板测试-CPU过高"], "skipped": []}

    # 生成的规则字段正确（device_type 被覆盖为 server_windows）
    rules = client.get("/api/alert/rules", headers=auth(admin_token)).json()
    rule = next(x for x in rules if x["name"] == "模板测试-CPU过高")
    assert (rule["metric"], rule["op"], rule["threshold"], rule["duration_cycles"],
            rule["severity"], rule["device_type"]) == \
        ("cpu_usage", ">", 88.0, 3, "major", "server_windows")
    assert rule["builtin"] is False

    # 二次：全 skipped，规则数不增
    n_rules = len(rules)
    r = client.post("/api/alert/templates/instantiate",
                    json={"template_ids": [tid]}, headers=auth(admin_token))
    assert r.json() == {"created": [], "skipped": ["模板测试-CPU过高"]}
    rules2 = client.get("/api/alert/rules", headers=auth(admin_token)).json()
    assert len(rules2) == n_rules

    # 内置模板：对应内置规则已播种存在 → skipped
    builtin = next(t for t in _templates() if t["name"] == "设备离线")
    r = client.post("/api/alert/templates/instantiate",
                    json={"template_ids": [builtin["id"]]}, headers=auth(admin_token))
    assert r.json()["skipped"] == ["设备离线"]


def test_instantiate_missing_template(client, admin_token):
    r = client.post("/api/alert/templates/instantiate",
                    json={"template_ids": [999999]}, headers=auth(admin_token))
    assert r.status_code == 404


def test_viewer_forbidden(client, viewer_token):
    """viewer 只读：列表可看，增删改与 instantiate 全部 403。"""
    r = client.get("/api/alert/templates", headers=auth(viewer_token))
    assert r.status_code == 200
    tid = r.json()[0]["id"]
    body = {"name": "模板测试-viewer", "metric": "cpu_usage", "op": ">",
            "threshold": 1, "severity": "info"}
    assert client.post("/api/alert/templates", json=body,
                       headers=auth(viewer_token)).status_code == 403
    assert client.put(f"/api/alert/templates/{tid}", json=body,
                      headers=auth(viewer_token)).status_code == 403
    assert client.delete(f"/api/alert/templates/{tid}",
                         headers=auth(viewer_token)).status_code == 403
    assert client.post("/api/alert/templates/instantiate", json={"template_ids": [tid]},
                       headers=auth(viewer_token)).status_code == 403


def test_template_validation(client, admin_token):
    base = {"name": "模板测试-校验", "metric": "cpu_usage", "threshold": 3,
            "severity": "warning"}
    # baseline_dev 阈值（N 倍标准差）必须 > 0
    r = client.post("/api/alert/templates", json={**base, "op": "baseline_dev", "threshold": 0},
                    headers=auth(admin_token))
    assert r.status_code == 422
    # baseline_dev 合法值可建（建完删掉，不污染会话库）
    r = client.post("/api/alert/templates", json={**base, "op": "baseline_dev", "threshold": 3},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["op"] == "baseline_dev"
    client.delete(f"/api/alert/templates/{tid}", headers=auth(admin_token))
    # 非法比较符
    r = client.post("/api/alert/templates", json={**base, "op": "≈"},
                    headers=auth(admin_token))
    assert r.status_code == 422
    # 去抖周期越界
    r = client.post("/api/alert/templates",
                    json={**base, "op": ">", "duration_cycles": 0},
                    headers=auth(admin_token))
    assert r.status_code == 422
