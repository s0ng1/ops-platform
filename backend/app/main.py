"""FastAPI 入口：建表、种子管理员、启动监控协程。"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import alert_templates, alerts, audits, auth, bus, config_backup, credentials, devices, discovery, ipam, logs, metrics, monitor, reports, topology, users, ws
from .core.config import get_settings
from .core.database import Base, SessionLocal, engine
from .core.logreceiver import start_log_receivers
from .core.security import hash_password
from .core.timescale import init_timescale
from .models import AlertRule, RuleTemplate, User
from .alerting.escalation import escalation_loop
from .scheduler.monitor_loop import monitor_loop
from .scheduler.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# 内置告警规则（启动时按名补种：已有同名规则跳过，用户删掉的内置规则重启后会补回）
_BUILTIN_RULES = [
    # 全部设备
    dict(name="设备离线", metric="device_online", op="==", threshold=0,
         duration_cycles=2, severity="critical"),
    dict(name="Ping 时延过高", metric="ping_latency_ms", op=">", threshold=200,
         duration_cycles=3, severity="warning"),
    # 网络/安全设备（选择器只支持单类型等值匹配，两类各种一套）
    dict(name="接口 down", metric="if_status", op="==", threshold=0,
         duration_cycles=2, severity="major", device_type="network"),
    dict(name="网络设备 CPU 使用率过高", metric="cpu_usage", op=">", threshold=80,
         duration_cycles=3, severity="major", device_type="network"),
    dict(name="网络设备 内存使用率过高", metric="mem_usage", op=">", threshold=85,
         duration_cycles=3, severity="major", device_type="network"),
    dict(name="网络设备 接口入向利用率过高", metric="if_in_util", op=">", threshold=80,
         duration_cycles=3, severity="warning", device_type="network"),
    dict(name="安全设备 CPU 使用率过高", metric="cpu_usage", op=">", threshold=80,
         duration_cycles=3, severity="major", device_type="security"),
    dict(name="安全设备 内存使用率过高", metric="mem_usage", op=">", threshold=85,
         duration_cycles=3, severity="major", device_type="security"),
    dict(name="安全设备 接口入向利用率过高", metric="if_in_util", op=">", threshold=80,
         duration_cycles=3, severity="warning", device_type="security"),
    # Windows/Linux 服务器
    dict(name="Windows 服务器 CPU 使用率过高", metric="cpu_usage", op=">", threshold=90,
         duration_cycles=3, severity="major", device_type="server_windows"),
    dict(name="Windows 服务器 内存使用率过高", metric="mem_usage", op=">", threshold=85,
         duration_cycles=3, severity="major", device_type="server_windows"),
    dict(name="Windows 服务器 磁盘使用率过高", metric="disk_usage", op=">", threshold=85,
         duration_cycles=2, severity="major", device_type="server_windows"),
    dict(name="Linux 服务器 CPU 使用率过高", metric="cpu_usage", op=">", threshold=90,
         duration_cycles=3, severity="major", device_type="server_linux"),
    dict(name="Linux 服务器 内存使用率过高", metric="mem_usage", op=">", threshold=85,
         duration_cycles=3, severity="major", device_type="server_linux"),
    dict(name="Linux 服务器 磁盘使用率过高", metric="disk_usage", op=">", threshold=85,
         duration_cycles=2, severity="major", device_type="server_linux"),
    # 数据库（MySQL 探针指标；无该指标的库种了也不响，无碍）
    dict(name="数据库 连接数使用率过高", metric="mysql_conn_usage_pct", op=">", threshold=80,
         duration_cycles=3, severity="major", device_type="database"),
    dict(name="数据库 主从延迟过高", metric="mysql_replication_delay", op=">", threshold=60,
         duration_cycles=3, severity="warning", device_type="database"),
    # Oracle / SQLServer 探针指标（无该指标的库种了也不响，无碍）
    dict(name="Oracle 表空间使用率过高", metric="oracle_tablespace_usage_pct", op=">", threshold=85,
         duration_cycles=3, severity="warning", device_type="database"),
    dict(name="Oracle 表空间使用率临界", metric="oracle_tablespace_usage_pct", op=">", threshold=95,
         duration_cycles=2, severity="major", device_type="database"),
    dict(name="SQLServer 缓存命中率过低", metric="mssql_buffer_cache_hit_pct", op="<", threshold=90,
         duration_cycles=3, severity="warning", device_type="database"),
    # PostgreSQL 探针指标
    dict(name="PostgreSQL 连接数使用率过高", metric="pg_conn_usage_pct", op=">", threshold=80,
         duration_cycles=3, severity="major", device_type="database"),
    dict(name="PostgreSQL 缓存命中率过低", metric="pg_cache_hit_ratio", op="<", threshold=90,
         duration_cycles=3, severity="warning", device_type="database"),
    # 配置备份变更（config_changed 点只由 config_backup 任务产生，无需设备类型选择器）
    dict(name="配置变更", metric="config_changed", op=">", threshold=0,
         duration_cycles=1, severity="info"),
    # 日志事件（log_event 点由日志接收器命中 log_rules 时产出，
    # labels.severity 区分等级，四级各种一条；labels_filter 见引擎标签过滤）
    dict(name="日志事件-致命", metric="log_event", op=">", threshold=0,
         duration_cycles=1, severity="critical", labels_filter={"severity": "critical"}),
    dict(name="日志事件-严重", metric="log_event", op=">", threshold=0,
         duration_cycles=1, severity="major", labels_filter={"severity": "major"}),
    dict(name="日志事件-警告", metric="log_event", op=">", threshold=0,
         duration_cycles=1, severity="warning", labels_filter={"severity": "warning"}),
    dict(name="日志事件-信息", metric="log_event", op=">", threshold=0,
         duration_cycles=1, severity="info", labels_filter={"severity": "info"}),
    # 新终端接入（new_terminal 点由 IPAM 采集/扫描回写在发现新 IP 时产生，labels 带 ip/mac）
    dict(name="新终端接入", metric="new_terminal", op=">", threshold=0,
         duration_cycles=1, severity="info"),
    # 应用拨测不可达（app_available 点由 app_probe 任务产生）
    dict(name="应用不可达", metric="app_available", op="==", threshold=0,
         duration_cycles=2, severity="major", device_type="application"),
    # Redis 内存使用率（redis_mem_usage_pct 只在配了 maxmemory 的实例上出点；Nginx 不加阈值规则，可用性已由上条覆盖）
    dict(name="Redis 内存使用率过高", metric="redis_mem_usage_pct", op=">", threshold=90,
         duration_cycles=2, severity="warning", device_type="application"),
]


def _seed_builtin_rules(db) -> int:
    """按名补种内置规则：同名（含用户自建的同名）规则跳过。返回新播种条数。"""
    existing = {name for (name,) in db.query(AlertRule.name).all()}
    new_rules = [r for r in _BUILTIN_RULES if r["name"] not in existing]
    for r in new_rules:
        db.add(AlertRule(builtin=True, **r))
    if new_rules:
        db.commit()
        log.info("已补种 %d 条内置告警规则", len(new_rules))
    return len(new_rules)


# 内置规则模板：与内置规则一一对应、**同名**（不加「模板」后缀——模板 instantiate 生成的
# 规则名 = 模板名，同名方案下生成物仍是规范命名的规则；模板与规则分属两表，同名不冲突。
# 副作用：内置规则未删时 instantiate 内置模板会全部 skipped，这正是「同名跳过」的幂等设计；
# 用户删掉内置规则后可用模板一键补回，与重启补种互为补充）。
_BUILTIN_TEMPLATES = [
    dict(r, description="内置模板：一键生成「%s」告警规则" % r["name"])
    for r in _BUILTIN_RULES
]


def _seed_builtin_templates(db) -> int:
    """按名补种内置规则模板（只增不改）：同名模板跳过。返回新播种条数。"""
    existing = {name for (name,) in db.query(RuleTemplate.name).all()}
    new_templates = [t for t in _BUILTIN_TEMPLATES if t["name"] not in existing]
    for t in new_templates:
        db.add(RuleTemplate(builtin=True, **t))
    if new_templates:
        db.commit()
        log.info("已补种 %d 条内置告警规则模板", len(new_templates))
    return len(new_templates)


def _init_db() -> None:
    Base.metadata.create_all(engine)
    init_timescale()  # 仅 PG 生效：metrics 升级 hypertable
    db = SessionLocal()
    try:
        settings = get_settings()
        if db.query(User).count() == 0:
            db.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                )
            )
            db.commit()
            log.info("已创建默认管理员 %s（请尽快修改密码）", settings.admin_username)
        _seed_builtin_rules(db)
        _seed_builtin_templates(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    stop_event = asyncio.Event()
    tasks = [asyncio.create_task(monitor_loop(stop_event))]
    tasks.append(asyncio.create_task(escalation_loop(stop_event)))
    tasks += start_scheduler(stop_event)
    # Syslog / SNMP Trap 接收器（启动失败静默降级，不阻塞主程序）
    log_receiver = None
    if get_settings().log_receiver_enabled:
        try:
            log_receiver = await start_log_receivers()
        except Exception:  # noqa: BLE001
            log.exception("日志接收器启动失败")
    yield
    stop_event.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if log_receiver is not None:
        log_receiver.close()


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)

# 开发期 Vite 跨域；生产由 nginx 同源反代
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, users.router, credentials.router, devices.router, discovery.router, monitor.router, metrics.router, alerts.router, alert_templates.router, audits.router, topology.router, reports.router, config_backup.router, logs.router, ipam.router, bus.router):
    app.include_router(r)
app.include_router(ws.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
