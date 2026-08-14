"""TimescaleDB 初始化：把 metrics 表升级为 hypertable 并配置压缩/保留/连续聚合。
仅 PostgreSQL 方言执行，全部语句幂等（失败静默记日志，不阻塞启动）。
SQLite 下完全跳过，metrics 保持普通表。
"""
import logging

from sqlalchemy import text

from .database import engine

log = logging.getLogger(__name__)

# 原始数据保留 30 天，7 天后压缩；5 分钟连续聚合保留 180 天（报表/长周期曲线用）
_DDL_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS timescaledb",
    # 旧版单 id 主键不满足 hypertable 分区要求，先升级/丢弃
    "ALTER TABLE metrics DROP CONSTRAINT IF EXISTS metrics_pkey",
    "SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE, migrate_data => TRUE)",
    "ALTER TABLE metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'device_id,metric')",
    "SELECT add_compression_policy('metrics', INTERVAL '7 days', if_not_exists => TRUE)",
    "SELECT add_retention_policy('metrics', INTERVAL '30 days', if_not_exists => TRUE)",
]

# 实时聚合：2.28 起新建 cagg 默认 materialized_only=true（只查已物化部分，看不到未物化的新数据），
# 显式关掉以保留「物化部分 + 原始表实时补全」的行为
_CAGG_SQL = """
CREATE MATERIALIZED VIEW metrics_5m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket(INTERVAL '5 minutes', time) AS bucket,
       device_id, metric, labels,
       avg(value) AS avg_value, max(value) AS max_value, min(value) AS min_value
FROM metrics
GROUP BY bucket, device_id, metric, labels
WITH NO DATA
"""

_CAGG_POLICIES = [
    "SELECT add_retention_policy('metrics_5m', INTERVAL '180 days', if_not_exists => TRUE)",
    """SELECT add_continuous_aggregate_policy('metrics_5m',
         start_offset => INTERVAL '1 hour', end_offset => INTERVAL '5 minutes',
         schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE)""",
    # 老库已建成的 view 补开实时聚合（幂等；老版本无此参数时失败静默，不可阻断启动）
    "ALTER MATERIALIZED VIEW metrics_5m SET (timescaledb.materialized_only = false)",
]

# Timescale 后台策略作业（压缩/保留/cagg 刷新）会拿 AccessExclusiveLock，
# 若与应用长事务跨进程循环等待会长时间卡死（第 4 期压测曾卡 ~300s）。
# 经查 2.28 源码（src/tsl 全量 grep），alter_job 的 config 并不识别 lock_timeout 键
# （写进去也不会生效），故退而用 ALTER ROLE 兜底：
# 影响面：应用角色（当前连接用户）的所有连接，任何锁等待超过 30s 即报错回滚当前语句。
# 应用正常查询毫秒级拿锁，仅在异常锁冲突时快速失败，避免僵死。
_JOB_LOCK_TIMEOUT_SQL = "ALTER ROLE CURRENT_USER SET lock_timeout = '30s'"


def init_timescale() -> None:
    """启动时调用；非 PG 或任一步失败均不影响应用运行。
    每条语句独立提交：避免单条失败回滚掉前面已成功的语句（如先丢旧主键再建 hypertable）。
    """
    if engine.dialect.name != "postgresql":
        return

    def run_each(statements: list[str], level: int) -> None:
        for stmt in statements:
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
            except Exception as e:  # noqa: BLE001
                log.log(level, "TimescaleDB 初始化语句失败 [%s]: %s", stmt.strip()[:60], e)

    run_each(_DDL_STATEMENTS, logging.WARNING)
    run_each([_CAGG_SQL], logging.DEBUG)  # 已存在时失败属正常
    run_each(_CAGG_POLICIES, logging.DEBUG)
    run_each([_JOB_LOCK_TIMEOUT_SQL], logging.WARNING)
    log.info("TimescaleDB hypertable 就绪（metrics）")
