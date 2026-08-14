"""数据库探针：只读监控账号直连采集，对应北塔的 JDBC 直连模式。
当前实现 MySQL（pymysql）/ Oracle（oracledb thin 模式，查 v$ 视图）/
SQLServer（pytds，查 DMV）/ PostgreSQL（psycopg3，查 pg_stat_database），
在 collect_db_metrics 里按 payload.db_type 分发。
驱动均为同步驱动，协程侧用 asyncio.to_thread 调用。
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from ..models import Device
from .rate import RateCalculator
from .snmp_metrics import MetricPoint

log = logging.getLogger(__name__)

DB_CONNECT_TIMEOUT = 5


def query_mysql_status_sync(host: str, payload: dict) -> dict:
    """同步查询 MySQL 全局状态，返回 {状态名: 值}。失败抛异常由调用方隔离。
    监控账号最小权限：USAGE + REPLICATION CLIENT（看主从）即可。
    """
    import pymysql

    conn = pymysql.connect(
        host=host,
        port=int(payload.get("port", 3306)),
        user=payload.get("username", ""),
        password=payload.get("password", ""),
        database=payload.get("database") or None,
        connect_timeout=DB_CONNECT_TIMEOUT,
        read_timeout=DB_CONNECT_TIMEOUT,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS")
            result = {name: value for name, value in cur.fetchall()}
            cur.execute("SHOW GLOBAL VARIABLES LIKE 'max_connections'")
            row = cur.fetchone()
            if row:
                result["max_connections"] = row[1]
            # 主从延迟：8.4+ 用 SHOW REPLICA STATUS，旧版 SHOW SLAVE STATUS
            delay = None
            for cmd, key in (("SHOW REPLICA STATUS", "Seconds_Behind_Source"),
                             ("SHOW SLAVE STATUS", "Seconds_Behind_Master")):
                try:
                    cur.execute(cmd)
                    row = cur.fetchone()
                    if row:
                        cols = [d[0] for d in cur.description]
                        delay = dict(zip(cols, row)).get(key)
                    break
                except Exception:  # noqa: BLE001 - 旧语法不支持则换下一种
                    continue
            if delay is not None:
                result["replication_delay"] = delay
            return result
    finally:
        conn.close()


async def collect_mysql_metrics(
    device: Device,
    payload: dict,
    rate_calc: RateCalculator,
    query=query_mysql_status_sync,
) -> list[MetricPoint]:
    """采集一台 MySQL 实例指标。query 可注入假数据便于测试。"""
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = datetime.now(timezone.utc).timestamp()
    host, did = device.ip, device.id

    try:
        status = await asyncio.to_thread(query, host, payload)
    except Exception as e:  # noqa: BLE001 - 连接失败本周期跳过
        log.debug("MySQL 探针失败 %s: %s", host, e)
        return points

    def put(metric: str, value, labels: dict | None = None) -> None:
        try:
            points.append(MetricPoint(did, metric, float(value), labels or {}, now))
        except (TypeError, ValueError):
            pass

    # 即时值
    put("mysql_threads_connected", status.get("Threads_connected"))
    put("mysql_threads_running", status.get("Threads_running"))
    put("mysql_max_connections", status.get("max_connections"))
    put("mysql_replication_delay", status.get("replication_delay"))
    # 连接数使用率（%），供「连接数使用率过高」告警规则直接按阈值判定
    try:
        pct = float(status["Threads_connected"]) / float(status["max_connections"]) * 100
        put("mysql_conn_usage_pct", round(pct, 2))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass

    # 速率值（累计计数器差值，首周期无值）
    def rate_of(name: str) -> float | None:
        try:
            raw = int(status[name])
        except (KeyError, TypeError, ValueError):
            return None
        return rate_calc.rate((did, "mysql", name), ts, raw)

    qps = rate_of("Queries")
    if qps is not None:
        put("mysql_qps", round(qps, 2))
    try:
        tps_raw = int(status.get("Com_commit", 0)) + int(status.get("Com_rollback", 0))
        tps = rate_calc.rate((did, "mysql", "tps"), ts, tps_raw)
        if tps is not None:
            put("mysql_tps", round(tps, 2))
    except (TypeError, ValueError):
        pass
    slow = rate_of("Slow_queries")
    if slow is not None:
        put("mysql_slow_qps", round(slow, 4))

    return points


def query_oracle_sync(host: str, payload: dict) -> dict:
    """同步查询 Oracle 实例状态（thin 模式，无需 Instant Client）。
    监控账号最小权限：CREATE SESSION + SELECT_CATALOG_ROLE（v$session/v$parameter/dba_* 只读）。
    服务名取 payload.service_name，缺省回退 database 字段。
    """
    import oracledb

    service = payload.get("service_name") or payload.get("database") or ""
    # 主机优先取凭据里的 host（一机多库/测试场景），缺省回退设备 IP（MySQL 分支同口径）
    db_host = payload.get("host") or host
    conn = oracledb.connect(
        user=payload.get("username", ""),
        password=payload.get("password", ""),
        dsn=f"{db_host}:{int(payload.get('port', 1521))}/{service}",
        tcp_connect_timeout=DB_CONNECT_TIMEOUT,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM v$session")
        sessions = cur.fetchone()[0]
        # 活动会话：仅前台用户会话（type='USER' 排除后台进程）
        cur.execute("SELECT COUNT(*) FROM v$session WHERE status = 'ACTIVE' AND type = 'USER'")
        active = cur.fetchone()[0]
        cur.execute("SELECT value FROM v$parameter WHERE name = 'sessions'")
        row = cur.fetchone()
        sessions_limit = row[0] if row else None
        # PERMANENT 表空间使用率（%）：总大小取 dba_data_files，空闲取 dba_free_space
        cur.execute(
            "SELECT ts.tablespace_name, "
            "ROUND((1 - NVL(fs.free_bytes, 0) / df.total_bytes) * 100, 2) "
            "FROM dba_tablespaces ts "
            "JOIN (SELECT tablespace_name, SUM(bytes) total_bytes "
            "      FROM dba_data_files GROUP BY tablespace_name) df "
            "  ON ts.tablespace_name = df.tablespace_name "
            "LEFT JOIN (SELECT tablespace_name, SUM(bytes) free_bytes "
            "           FROM dba_free_space GROUP BY tablespace_name) fs "
            "  ON ts.tablespace_name = fs.tablespace_name "
            "WHERE ts.contents = 'PERMANENT' AND ts.status = 'ONLINE'"
        )
        tablespaces = [(name, pct) for name, pct in cur.fetchall()]
        return {
            "sessions": sessions,
            "sessions_limit": sessions_limit,
            "active_sessions": active,
            "tablespaces": tablespaces,
        }
    finally:
        conn.close()


async def collect_oracle_metrics(
    device: Device,
    payload: dict,
    rate_calc: RateCalculator,
    query=query_oracle_sync,
) -> list[MetricPoint]:
    """采集一台 Oracle 实例指标。query 可注入假数据便于测试。"""
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    host, did = device.ip, device.id

    t0 = time.monotonic()
    try:
        status = await asyncio.to_thread(query, host, payload)
    except Exception as e:  # noqa: BLE001 - 连接失败本周期静默，只出可用性 0
        log.debug("Oracle 探针失败 %s: %s", host, e)
        points.append(MetricPoint(did, "db_available", 0.0, {}, now))
        return points
    latency_ms = (time.monotonic() - t0) * 1000

    def put(metric: str, value, labels: dict | None = None) -> None:
        try:
            points.append(MetricPoint(did, metric, float(value), labels or {}, now))
        except (TypeError, ValueError):
            pass

    # 基础指标：实例可用性 + 查询耗时（与 SQLServer 分支同口径）
    put("db_available", 1)
    put("db_latency", round(latency_ms, 1))

    put("oracle_sessions", status.get("sessions"))
    put("oracle_active_sessions", status.get("active_sessions"))
    # 会话数使用率（%），供告警规则直接按阈值判定
    try:
        pct = float(status["sessions"]) / float(status["sessions_limit"]) * 100
        put("oracle_sessions_usage_pct", round(pct, 2))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    for ts_name, used_pct in status.get("tablespaces") or []:
        put("oracle_tablespace_usage_pct", used_pct, {"tablespace": ts_name})

    return points


def query_mssql_sync(host: str, payload: dict) -> dict:
    """同步查询 SQLServer 实例状态（pytds 纯 Python 驱动）。
    监控账号最小权限：VIEW SERVER STATE（看 DMV）即可。
    返回原始计数器值，命中率 ratio/base 相除与速率差值在协程侧做。
    """
    import pytds

    conn = pytds.connect(
        # 主机优先取凭据里的 host（一机多库/测试场景），缺省回退设备 IP（MySQL 分支同口径）
        server=payload.get("host") or host,
        port=int(payload.get("port", 1433)),
        database=payload.get("database") or "master",
        user=payload.get("username", ""),
        password=payload.get("password", ""),
        login_timeout=DB_CONNECT_TIMEOUT,
        timeout=DB_CONNECT_TIMEOUT,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.dm_exec_connections")
        connections = cur.fetchone()[0]
        # 缓存命中率（ratio/base 两行）与批请求累计值都在性能计数器 DMV 里
        cur.execute(
            "SELECT counter_name, cntr_value FROM sys.dm_os_performance_counters "
            "WHERE counter_name IN "
            "('Buffer cache hit ratio', 'Buffer cache hit ratio base', 'Batch Requests/sec')"
        )
        # counter_name 是定长 nchar，真机返回带尾部空格，键名必须 strip（mock 测不出来）
        counters = {name.strip(): value for name, value in cur.fetchall()}
        return {
            "connections": connections,
            "bchr": counters.get("Buffer cache hit ratio"),
            "bchr_base": counters.get("Buffer cache hit ratio base"),
            "batch_requests": counters.get("Batch Requests/sec"),
        }
    finally:
        conn.close()


async def collect_mssql_metrics(
    device: Device,
    payload: dict,
    rate_calc: RateCalculator,
    query=query_mssql_sync,
) -> list[MetricPoint]:
    """采集一台 SQLServer 实例指标。query 可注入假数据便于测试。"""
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = datetime.now(timezone.utc).timestamp()
    host, did = device.ip, device.id

    t0 = time.monotonic()
    try:
        status = await asyncio.to_thread(query, host, payload)
    except Exception as e:  # noqa: BLE001 - 连接失败本周期静默，只出可用性 0
        log.debug("SQLServer 探针失败 %s: %s", host, e)
        points.append(MetricPoint(did, "db_available", 0.0, {}, now))
        return points
    latency_ms = (time.monotonic() - t0) * 1000

    def put(metric: str, value, labels: dict | None = None) -> None:
        try:
            points.append(MetricPoint(did, metric, float(value), labels or {}, now))
        except (TypeError, ValueError):
            pass

    # 基础指标：实例可用性 + 查询耗时（与 Oracle 分支同口径）
    put("db_available", 1)
    put("db_latency", round(latency_ms, 1))

    put("mssql_connections", status.get("connections"))
    # Buffer cache hit ratio 是比值型计数器：cntr_value 须除以同名 base 行
    try:
        pct = float(status["bchr"]) / float(status["bchr_base"]) * 100
        put("mssql_buffer_cache_hit_pct", round(pct, 2))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    # Batch Requests/sec 是累计计数器，跨周期差值（首周期无值）
    try:
        raw = int(status["batch_requests"])
        rate = rate_calc.rate((did, "mssql", "batch_requests"), ts, raw)
        if rate is not None:
            put("mssql_batch_per_sec", round(rate, 2))
    except (TypeError, ValueError):
        pass

    return points


def query_postgres_sync(host: str, payload: dict) -> dict:
    """同步查询 PostgreSQL 实例状态（psycopg3 驱动，与平台自身数据库同源）。
    监控账号最小权限：pg_monitor 角色（或 CONNECT + pg_stat_database 只读）。
    返回 {connections, max_connections, blks_hit, blks_read}。
    """
    import psycopg

    # 主机优先取凭据里的 host（一机多库/测试场景），缺省回退设备 IP（其余分支同口径）
    db_host = payload.get("host") or host
    conn = psycopg.connect(
        host=db_host,
        port=int(payload.get("port", 5432)),
        user=payload.get("username", ""),
        password=payload.get("password", ""),
        dbname=payload.get("database") or None,
        connect_timeout=DB_CONNECT_TIMEOUT,
    )
    try:
        with conn.cursor() as cur:
            # 连接数：各库 numbackends 之和（客户端连接，不含后台进程）
            cur.execute("SELECT COALESCE(sum(numbackends), 0) FROM pg_stat_database")
            connections = int(cur.fetchone()[0])
            cur.execute("SHOW max_connections")
            max_connections = int(cur.fetchone()[0])
            # 缓存命中：blks_hit / (blks_hit + blks_read) 跨库累计
            cur.execute("SELECT COALESCE(sum(blks_hit), 0), COALESCE(sum(blks_read), 0) FROM pg_stat_database")
            blks_hit, blks_read = (int(v) for v in cur.fetchone())
            return {
                "connections": connections,
                "max_connections": max_connections,
                "blks_hit": blks_hit,
                "blks_read": blks_read,
            }
    finally:
        conn.close()


async def collect_postgres_metrics(
    device: Device,
    payload: dict,
    rate_calc: RateCalculator,
    query=query_postgres_sync,
) -> list[MetricPoint]:
    """采集一台 PostgreSQL 实例指标。query 可注入假数据便于测试。"""
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    host, did = device.ip, device.id

    t0 = time.monotonic()
    try:
        status = await asyncio.to_thread(query, host, payload)
    except Exception as e:  # noqa: BLE001 - 连接失败本周期静默，只出可用性 0
        log.debug("PostgreSQL 探针失败 %s: %s", host, e)
        points.append(MetricPoint(did, "db_available", 0.0, {}, now))
        return points
    latency_ms = (time.monotonic() - t0) * 1000

    def put(metric: str, value, labels: dict | None = None) -> None:
        try:
            points.append(MetricPoint(did, metric, float(value), labels or {}, now))
        except (TypeError, ValueError):
            pass

    # 基础指标：实例可用性 + 查询耗时（与 Oracle/SQLServer 分支同口径）
    put("db_available", 1)
    put("db_latency", round(latency_ms, 1))

    put("pg_connections", status.get("connections"))
    put("pg_max_connections", status.get("max_connections"))
    # 连接数使用率（%），供「连接数使用率过高」告警规则直接按阈值判定
    try:
        pct = float(status["connections"]) / float(status["max_connections"]) * 100
        put("pg_conn_usage_pct", round(pct, 2))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    # 缓存命中率（%）：命中块 / 总请求块
    try:
        hit = float(status["blks_hit"])
        read = float(status["blks_read"])
        total = hit + read
        if total > 0:
            put("pg_cache_hit_ratio", round(hit / total * 100, 2))
    except (KeyError, TypeError, ValueError):
        pass

    return points


async def collect_db_metrics(
    device: Device, payload: dict, rate_calc: RateCalculator, **kwargs
) -> list[MetricPoint]:
    """按 db_type 分发到具体探针；未支持的类型记日志跳过。"""
    db_type = payload.get("db_type", "mysql")
    if db_type == "mysql":
        return await collect_mysql_metrics(device, payload, rate_calc, **kwargs)
    if db_type == "oracle":
        return await collect_oracle_metrics(device, payload, rate_calc, **kwargs)
    if db_type == "sqlserver":
        return await collect_mssql_metrics(device, payload, rate_calc, **kwargs)
    if db_type == "postgresql":
        return await collect_postgres_metrics(device, payload, rate_calc, **kwargs)
    # db2 等其余类型按现场数据库种类补充
    log.debug("暂不支持的数据库类型 %s（%s）", db_type, device.ip)
    return []
