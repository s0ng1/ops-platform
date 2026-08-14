"""内置告警规则播种：按名补种（幂等）、同名跳过不冲突、新规则字段正确。"""
from app.core.database import SessionLocal
from app.main import _BUILTIN_RULES, _init_db
from app.models import AlertRule


def _rules():
    db = SessionLocal()
    rows = db.query(AlertRule).all()
    db.close()
    return rows


def _clear_rules():
    db = SessionLocal()
    db.query(AlertRule).delete()
    db.commit()
    db.close()


def test_seed_idempotent(client):
    """清空后播种出全量内置规则；连跑两次数量不变。"""
    _clear_rules()
    _init_db()
    n1 = len(_rules())
    assert n1 == len(_BUILTIN_RULES)
    _init_db()
    assert len(_rules()) == n1


def test_seed_skips_existing_same_name(client):
    """已有同名规则（用户自建）不冲突：跳过不重复、不覆盖其字段。"""
    _clear_rules()
    db = SessionLocal()
    db.add(AlertRule(name="设备离线", metric="device_online", op="==", threshold=0,
                     duration_cycles=5, severity="warning"))
    db.commit()
    db.close()
    _init_db()
    rows = _rules()
    same_name = [r for r in rows if r.name == "设备离线"]
    assert len(same_name) == 1
    assert same_name[0].builtin is False       # 用户自建的同名规则不被覆盖
    assert same_name[0].duration_cycles == 5
    assert len(rows) == len(_BUILTIN_RULES)    # 其余内置规则照常补齐


def test_builtin_rule_fields(client):
    """新增内置规则的 op/threshold/duration_cycles/severity/device_type 正确。"""
    _clear_rules()
    _init_db()
    by_name = {r.name: r for r in _rules()}
    # (规则名, metric, op, threshold, duration_cycles, severity, device_type)
    expected = [
        ("Ping 时延过高", "ping_latency_ms", ">", 200, 3, "warning", ""),
        ("网络设备 CPU 使用率过高", "cpu_usage", ">", 80, 3, "major", "network"),
        ("网络设备 内存使用率过高", "mem_usage", ">", 85, 3, "major", "network"),
        ("网络设备 接口入向利用率过高", "if_in_util", ">", 80, 3, "warning", "network"),
        ("安全设备 CPU 使用率过高", "cpu_usage", ">", 80, 3, "major", "security"),
        ("安全设备 内存使用率过高", "mem_usage", ">", 85, 3, "major", "security"),
        ("安全设备 接口入向利用率过高", "if_in_util", ">", 80, 3, "warning", "security"),
        ("Windows 服务器 CPU 使用率过高", "cpu_usage", ">", 90, 3, "major", "server_windows"),
        ("Windows 服务器 内存使用率过高", "mem_usage", ">", 85, 3, "major", "server_windows"),
        ("Windows 服务器 磁盘使用率过高", "disk_usage", ">", 85, 2, "major", "server_windows"),
        ("Linux 服务器 CPU 使用率过高", "cpu_usage", ">", 90, 3, "major", "server_linux"),
        ("Linux 服务器 内存使用率过高", "mem_usage", ">", 85, 3, "major", "server_linux"),
        ("Linux 服务器 磁盘使用率过高", "disk_usage", ">", 85, 2, "major", "server_linux"),
        ("数据库 连接数使用率过高", "mysql_conn_usage_pct", ">", 80, 3, "major", "database"),
        ("数据库 主从延迟过高", "mysql_replication_delay", ">", 60, 3, "warning", "database"),
        ("Oracle 表空间使用率过高", "oracle_tablespace_usage_pct", ">", 85, 3, "warning", "database"),
        ("Oracle 表空间使用率临界", "oracle_tablespace_usage_pct", ">", 95, 2, "major", "database"),
        ("SQLServer 缓存命中率过低", "mssql_buffer_cache_hit_pct", "<", 90, 3, "warning", "database"),
        ("PostgreSQL 连接数使用率过高", "pg_conn_usage_pct", ">", 80, 3, "major", "database"),
        ("PostgreSQL 缓存命中率过低", "pg_cache_hit_ratio", "<", 90, 3, "warning", "database"),
    ]
    for name, metric, op, threshold, cycles, severity, device_type in expected:
        r = by_name.get(name)
        assert r is not None, f"缺少内置规则 {name}"
        assert (r.metric, r.op, r.threshold, r.duration_cycles, r.severity, r.device_type) == \
            (metric, op, threshold, cycles, severity, device_type), f"规则字段不符 {name}"
        assert r.builtin is True
