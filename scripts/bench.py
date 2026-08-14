"""M4 压测：stub 网络层，压「调度器 + 监控循环 + 告警引擎 + DB 写入」真实链路。

不真实采集（无 SNMP/SSH/MySQL/ping 网络流量），各采集器在既有注入点
（fetch_walk/run_commands/query）换成合成数据，单点耗时可配置（默认 5~20ms
模拟 LAN 延迟）；ping 与 SNMP 系统信息同样 stub。调度器、告警引擎、metrics
入库全部走真实代码。

数据隔离：默认使用独立的 ops_bench 库（自动建库），跑完 DROP，绝不碰 dev 的 ops 库。

用法（在项目根执行）：
    OPS_DB_PASSWORD=xxx backend/.venv/python scripts/bench.py --devices 2000 --cycles 3
    # 或显式指定连接串（库名会被替换为 --dbname）：
    OPS_BENCH_DATABASE_URL=postgresql+psycopg://ops:xxx@127.0.0.1:5432/ops_bench \
        backend/.venv/python scripts/bench.py --devices 500

结果：终端打印表格 + --json 落盘（供压测报告汇总）。
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import getpass
import json
import os
import platform
import random
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

# 设备类型配比（采集矩阵）：网络 40% / 安全 10% / Windows 25% / Linux 20% / 数据库 5%
TYPE_MIX = [
    ("network", 0.40),
    ("security", 0.10),
    ("server_windows", 0.25),
    ("server_linux", 0.20),
    ("database", 0.05),
]

# 与 tests/conftest.py 相同的固定测试密钥（bench 库自造凭据，与 dev 库密钥无关）
TEST_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


# ---------------------------------------------------------------- 环境准备


def _read_deploy_env_password() -> str:
    env_file = ROOT / "deploy" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("OPS_DB_PASSWORD="):
                return line.split("=", 1)[1].strip()
    return ""


def resolve_urls(dbname: str) -> tuple[str, str]:
    """返回 (admin_url, bench_url)。admin_url 连 ops 库用于建/删 bench 库。"""
    raw = os.environ.get("OPS_BENCH_DATABASE_URL") or os.environ.get("OPS_DATABASE_URL")
    if raw:
        from sqlalchemy.engine import make_url

        url = make_url(raw)
        admin_url = url.set(database="ops")
        bench_url = url.set(database=dbname)
    else:
        password = os.environ.get("OPS_DB_PASSWORD") or _read_deploy_env_password()
        if not password:
            password = getpass.getpass("OPS_DB_PASSWORD: ")
        host = os.environ.get("OPS_DB_HOST", "127.0.0.1")
        port = os.environ.get("OPS_DB_PORT", "5432")
        admin_url = f"postgresql+psycopg://ops:{password}@{host}:{port}/ops"
        bench_url = f"postgresql+psycopg://ops:{password}@{host}:{port}/{dbname}"
    if not str(bench_url).startswith("postgresql"):
        raise SystemExit("压测必须指向 PostgreSQL（TimescaleDB hypertable 链路）")
    if str(bench_url).rstrip("/").endswith("/ops"):
        raise SystemExit("拒绝在 dev 业务库 ops 上压测，请换 --dbname")
    return str(admin_url), str(bench_url)


def ensure_database(admin_url: str, dbname: str) -> None:
    """bench 库不存在则创建（ops 用户在 docker 里是超级用户，同 conftest 先例）。"""
    from sqlalchemy import create_engine, text

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin.dispose()


def drop_database(admin_url: str, dbname: str) -> None:
    from sqlalchemy import create_engine, text

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    admin.dispose()


def rss_mb() -> float:
    """当前进程 RSS（Linux 读 /proc，兜底 resource）。"""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


# ---------------------------------------------------------------- 合成数据（stub 网络层）


class FakeNet:
    """各采集器注入点用的假网络层：可配置单点延迟，计数器跨周期递增（速率指标可算）。"""

    def __init__(self, latency_ms: tuple[float, float], ifaces: int, mods: dict):
        self.lmin, self.lmax = latency_ms
        self.ifaces = ifaces
        self.m = mods  # 采集器模块引用（OID 常量）
        self.counters: dict[tuple, int] = {}
        self.jiffies: dict[str, tuple[int, int]] = {}
        self.mysql_counters: dict[str, dict[str, int]] = {}

    def _lat_s(self) -> float:
        return random.uniform(self.lmin, self.lmax) / 1000

    async def _asleep(self) -> None:
        await asyncio.sleep(self._lat_s())

    def _bump(self, key: tuple, lo: int, hi: int) -> int:
        v = self.counters.get(key, random.randint(0, 1 << 30))
        v = (v + random.randint(lo, hi)) % (1 << 64)
        self.counters[key] = v
        return v

    # ---- SNMP walk（网络/安全设备接口 + 设备级 + Windows 全量）----
    async def walk(self, host: str, payload: dict, oid: str) -> dict[str, str]:
        await self._asleep()
        sm, wm = self.m["snmp_metrics"], self.m["windows"]
        n = self.ifaces
        if oid == sm.OID_IF_NAME:
            return {f"{oid}.{i}": f"GigabitEthernet0/{i}" for i in range(1, n + 1)}
        if oid == sm.OID_IF_HC_IN:
            return {f"{oid}.{i}": str(self._bump((host, "in", i), 1 << 20, 1 << 26)) for i in range(1, n + 1)}
        if oid == sm.OID_IF_HC_OUT:
            return {f"{oid}.{i}": str(self._bump((host, "out", i), 1 << 20, 1 << 26)) for i in range(1, n + 1)}
        if oid == sm.OID_IF_HIGH_SPEED:
            return {f"{oid}.{i}": "1000" for i in range(1, n + 1)}
        if oid == sm.OID_IF_OPER_STATUS:
            return {f"{oid}.{i}": "1" for i in range(1, n + 1)}
        # 设备级 CPU/内存（Cisco walk_avg + pool_pct）
        if oid == "1.3.6.1.4.1.9.9.109.1.1.1.1.8":  # cpu walk_avg
            return {f"{oid}.1": str(random.randint(5, 95))}
        if oid == "1.3.6.1.4.1.9.9.48.1.1.1.5":  # mem used
            return {f"{oid}.1": str(random.randint(200_000_000, 800_000_000))}
        if oid == "1.3.6.1.4.1.9.9.48.1.1.1.6":  # mem free
            return {f"{oid}.1": str(random.randint(100_000_000, 400_000_000))}
        # Windows HOST-RESOURCES-MIB
        if oid == wm.OID_HR_PROCESSOR_LOAD:
            return {f"{oid}.{i}": str(random.randint(1, 95)) for i in range(1, 5)}
        if oid == wm.OID_HR_STORAGE_TYPE:
            return {f"{oid}.1": wm.TYPE_RAM, f"{oid}.2": wm.TYPE_FIXED_DISK, f"{oid}.3": wm.TYPE_FIXED_DISK}
        if oid == wm.OID_HR_STORAGE_DESCR:
            return {f"{oid}.1": "Physical Memory", f"{oid}.2": "C:\\", f"{oid}.3": "D:\\"}
        if oid == wm.OID_HR_STORAGE_UNITS:
            return {f"{oid}.{i}": "4096" for i in range(1, 4)}
        if oid == wm.OID_HR_STORAGE_SIZE:
            return {f"{oid}.{i}": str(4 << 20) for i in range(1, 4)}
        if oid == wm.OID_HR_STORAGE_USED:
            return {f"{oid}.{i}": str(random.randint(1 << 19, 3 << 20)) for i in range(1, 4)}
        if oid == wm.OID_HR_SW_RUN_INDEX:
            return {f"{oid}.{i}": str(i) for i in range(1, 151)}
        # IPAM：ARP 表 + FDB 表（第 6 期新增采集器；每台 6 条固定记录，
        # 跨设备重复走 upsert 更新路径，ip_inventory 行数不随设备数膨胀）
        im = self.m["ipam"]
        if oid == im.OID_ARP_MAC:
            # 后缀 .<ifIndex>.<IP 四段>，值 0x hex MAC（与 FDB 后缀十进制一一对应）
            return {f"{oid}.2.10.0.{i}.{16 + j}": f"0x02000a{i:02x}{(16 + j):02x}"
                    for i in range(3) for j in range(2)}
        if oid == im.OID_FDB_PORT:
            # 后缀 .<MAC 六段十进制>，值端口 ifIndex（2 → ifName GigabitEthernet0/2）
            return {f"{oid}.0.32.0.10.{i}.{16 + j}": "2" for i in range(3) for j in range(2)}
        return {}

    async def get_multi(self, host: str, payload: dict, oids: list[str]) -> dict[str, str]:
        await self._asleep()
        return {}

    # ---- Linux SSH ----
    async def run_commands(self, host: str, payload: dict, commands: list[str]) -> list[str]:
        await self._asleep()
        busy, total = self.jiffies.get(host, (random.randint(0, 1 << 20), 1 << 24))
        busy += random.randint(50, 500)
        total += random.randint(900, 1100)
        self.jiffies[host] = (busy, total)
        idle = total - busy
        stat = f"cpu  {busy // 3} 0 {busy // 3} {idle} 0 0 {busy // 3} 0 0 0\n"
        meminfo = "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n"
        df = (
            "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
            f"/dev/sda1        102400000 40960000  61440000  {random.randint(30, 90)}% /\n"
            f"/dev/sdb1        204800000 81920000 122880000  {random.randint(30, 90)}% /data\n"
        )
        load = f"{random.uniform(0, 4):.2f} {random.uniform(0, 4):.2f} {random.uniform(0, 4):.2f} 1/200 12345\n"
        return [stat, meminfo, df, load]

    # ---- MySQL 探针（同步驱动，asyncio.to_thread 调用）----
    def mysql_query(self, host: str, payload: dict) -> dict:
        time.sleep(self._lat_s())
        c = self.mysql_counters.setdefault(host, {"Queries": 1 << 24, "Com_commit": 1 << 20, "Slow_queries": 100})
        c["Queries"] += random.randint(500, 5000)
        c["Com_commit"] += random.randint(100, 1000)
        c["Slow_queries"] += random.randint(0, 3)
        return {
            "Threads_connected": str(random.randint(5, 200)),
            "Threads_running": str(random.randint(1, 20)),
            "max_connections": "500",
            **{k: str(v) for k, v in c.items()},
        }

    # ---- ping / SNMP 系统信息（监控循环）----
    async def ping(self, ip: str, timeout: int = 1) -> tuple[bool, int | None]:
        await self._asleep()
        return True, random.randint(1, 20)

    async def get_system_info(self, ip: str, payload: dict) -> dict:
        await self._asleep()
        return {"sys_descr": "bench device", "sys_object_id": "1.3.6.1.4.1.9.1.1", "sys_name": ""}


# ---------------------------------------------------------------- 主流程


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M4 压测：调度器+告警引擎+DB 写入链路")
    p.add_argument("--devices", type=int, default=2000, help="模拟设备数（默认 2000）")
    p.add_argument("--cycles", type=int, default=3, help="完整采集周期数（默认 3）")
    p.add_argument("--ifaces", type=int, default=8, help="网络/安全设备接口数（默认 8）")
    p.add_argument("--latency-min", type=float, default=5.0, help="单点延迟下限 ms（默认 5）")
    p.add_argument("--latency-max", type=float, default=20.0, help="单点延迟上限 ms（默认 20）")
    p.add_argument("--concurrency", type=int, default=0, help="覆盖协程池并发上限（默认用调度器现有值 50）")
    p.add_argument("--pool-size", type=int, default=0,
                   help="重配 DB 连接池 pool_size/max_overflow（默认 0=保持 OPS_DB_POOL_SIZE/OPS_DB_MAX_OVERFLOW 配置，默认 20+40）")
    p.add_argument("--dbname", default="ops_bench", help="独立压测库名（默认 ops_bench）")
    p.add_argument("--keep", action="store_true", help="跑完保留 bench 库（默认 DROP）")
    p.add_argument("--json", dest="json_path", default="", help="结果 JSON 落盘路径")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


async def run_bench(args: argparse.Namespace) -> dict:
    # --- 在导入 app 之前指向 bench 库与固定测试密钥 ---
    admin_url, bench_url = resolve_urls(args.dbname)
    ensure_database(admin_url, args.dbname)
    os.environ["OPS_DATABASE_URL"] = bench_url
    os.environ["OPS_FERNET_KEY"] = TEST_FERNET_KEY
    sys.path.insert(0, str(BACKEND))

    import logging

    logging.basicConfig(level=logging.WARNING)

    from app.collectors import config_backup, db_probe, ipam, linux_ssh, scanner, snmp, snmp_metrics, windows
    from app.collectors.rate import RateCalculator
    from app.alerting import engine as alert_engine
    from app.core.database import SessionLocal, engine
    from app.main import _init_db
    from app.models import Credential, Device
    from app.scheduler import monitor_loop, scheduler as sched

    _init_db()  # 建表 + hypertable + 内置告警规则播种（与生产启动一致）

    # 摘掉策略后台作业（retention/compression/cagg 刷新）：建策略后首跑作业会对
    # metrics chunk 请求 AccessExclusiveLock，与压测并发会话的「idle in transaction」
    # 长事务形成锁排队，曾把首周期卡死 ~300s（锁队列堵住事件循环里的同步 SELECT →
    # 持事务协程无法恢复 → 循环等待）。10 分钟级压测用不到保留/压缩/聚合策略；
    # hypertable 结构与写入路径完全一致。该碰撞作为生产风险写入报告。
    from sqlalchemy import text as _text

    with engine.begin() as conn:
        for stmt in (
            "SELECT remove_retention_policy('metrics', if_exists => TRUE)",
            "SELECT remove_compression_policy('metrics', if_exists => TRUE)",
            "SELECT remove_continuous_aggregate_policy('metrics_5m', if_exists => TRUE)",
        ):
            try:
                conn.execute(_text(stmt))
            except Exception as e:  # noqa: BLE001 - 幂等，失败不影响压测
                print(f"[init] 策略移除失败（忽略）: {e}", flush=True)

    # 兜底：等残留策略作业结束再计时
    t_wait = time.perf_counter()
    while True:
        with engine.connect() as conn:
            n_jobs = conn.execute(_text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE '%_timescaledb_functions.policy%' AND state = 'active' "
                "AND pid <> pg_backend_pid()"  # 排除本查询自身（LIKE 模式字面量会自匹配）
            )).scalar()
        if not n_jobs:
            break
        if time.perf_counter() - t_wait > 600:
            raise SystemExit("等待 Timescale 策略作业结束超时（600s）")
        time.sleep(2)
    wait_s = time.perf_counter() - t_wait
    if wait_s > 1:
        print(f"[init] 等待 Timescale 策略作业结束: {wait_s:.0f}s", flush=True)

    # 可选：重配连接池（生产代码默认走 OPS_DB_POOL_SIZE/OPS_DB_MAX_OVERFLOW=20+40，压测时
    # 用来分层定位瓶颈；不重配即完全生产口径）。SessionLocal.configure 改绑新引擎，不改业务代码。
    bench_engine = engine
    if args.pool_size:
        from sqlalchemy import create_engine

        bench_engine = create_engine(
            bench_url, pool_pre_ping=True,
            pool_size=args.pool_size, max_overflow=args.pool_size,
        )
        SessionLocal.configure(bind=bench_engine)

    from app.core.config import get_settings

    pool_desc = args.pool_size or (
        f"默认({get_settings().db_pool_size}+{get_settings().db_max_overflow})"
    )

    if args.concurrency:
        sched.COLLECT_CONCURRENCY = args.concurrency
        monitor_loop.CHECK_CONCURRENCY = args.concurrency

    # --- stub 网络层（各采集器既有注入点；调度器内引用名做模块级替换）---
    net = FakeNet((args.latency_min, args.latency_max), args.ifaces,
                  {"snmp_metrics": snmp_metrics, "windows": windows, "ipam": ipam})
    sched.collect_device_metrics = functools.partial(
        snmp_metrics.collect_device_metrics, fetch_walk=net.walk, fetch_get=net.get_multi)
    sched.collect_windows_metrics = functools.partial(windows.collect_windows_metrics, fetch_walk=net.walk)
    sched.collect_linux_metrics = functools.partial(linux_ssh.collect_linux_metrics, run_commands=net.run_commands)
    sched.collect_db_metrics = functools.partial(db_probe.collect_db_metrics, query=net.mysql_query)
    # 第 6 期新增：配置备份与 IPAM 采集（不 stub 会对假 IP 做真实 SSH/SNMP 直到超时，
    # 曾把首周期拖到 777s）。app_probe 无需 stub——bench 设备配比不含 application 类型。
    sched.collect_config_backup = functools.partial(
        config_backup.collect_config_backup, run_commands=net.run_commands)
    sched.collect_ipam = functools.partial(ipam.collect_ipam, fetch_walk=net.walk)
    scanner.ping = net.ping
    snmp.get_system_info = net.get_system_info

    # --- 告警引擎计时插桩 ---
    eval_stats = {"calls": 0, "points": 0, "time": 0.0}
    orig_eval = alert_engine.evaluate_points

    async def timed_eval(points):
        t0 = time.perf_counter()
        await orig_eval(points)
        eval_stats["calls"] += 1
        eval_stats["points"] += len(points)
        eval_stats["time"] += time.perf_counter() - t0

    alert_engine.evaluate_points = timed_eval

    # --- 造数：3 条 dummy 凭据 + N 台 bench- 设备 ---
    db = SessionLocal()
    try:
        creds = {}
        for kind, payload in (
            ("snmp_v2c", {"community": "public", "port": 161}),
            ("ssh", {"username": "bench", "password": "bench", "port": 22}),
            ("database", {"username": "bench", "password": "bench", "port": 3306, "db_type": "mysql"}),
        ):
            c = Credential(name=f"bench-{kind}", kind=kind)
            c.set_payload(payload)
            db.add(c)
            creds[kind] = c
        db.flush()

        type_cred = {
            "network": creds["snmp_v2c"], "security": creds["snmp_v2c"],
            "server_windows": creds["snmp_v2c"], "server_linux": creds["ssh"],
            "database": creds["database"],
        }
        counts: dict[str, int] = {}
        batch = []
        for i in range(args.devices):
            r = (i + 0.5) / args.devices
            acc = 0.0
            dtype = TYPE_MIX[-1][0]
            for t, ratio in TYPE_MIX:
                acc += ratio
                if r <= acc:
                    dtype = t
                    break
            counts[dtype] = counts.get(dtype, 0) + 1
            batch.append(Device(
                name=f"bench-{i:05d}",
                ip=f"10.255.{i // 250}.{(i % 250) + 1}",
                type=dtype,
                credential=type_cred[dtype],
                monitor_enabled=True,
                # network 给 Cisco 指纹让设备级 CPU/内存也走真实采集路径
                sys_object_id="1.3.6.1.4.1.9.1.1" if dtype == "network" else "",
            ))
            if len(batch) >= 500:
                db.add_all(batch)
                db.commit()
                batch = []
        if batch:
            db.add_all(batch)
            db.commit()
    finally:
        db.close()

    # --- 执行若干完整周期：监控循环 + 调度器任务并发（同生产 lifespan）---
    ctx = {"rate_calc": RateCalculator()}
    cycles_detail = []
    for cycle in range(args.cycles):
        eval_stats.update(calls=0, points=0, time=0.0)
        t0 = time.perf_counter()

        async def timed_monitor():
            t = time.perf_counter()
            await monitor_loop.run_check_once()
            return {"time": time.perf_counter() - t}

        async def timed_task(task):
            t = time.perf_counter()
            n = await sched.run_task_once(task, ctx)
            return {"points": n, "time": time.perf_counter() - t}

        aws = [("monitor", timed_monitor())]
        for task in sched.TASKS:
            # 300s 周期任务每 5 个 60s 周期跑 1 次（与生产节奏一致）
            if task.interval <= 60 or cycle % 5 == 0:
                aws.append((task.name, timed_task(task)))
        results = await asyncio.gather(*(aw for _, aw in aws))
        wall = time.perf_counter() - t0
        detail = {
            "cycle": cycle,
            "wall_s": round(wall, 2),
            "monitor": results[0],
            "tasks": {name: res for (name, _), res in zip(aws, results) if name != "monitor"},
            "eval": dict(eval_stats),
            "rss_mb": round(rss_mb(), 1),
        }
        detail["db_rows"] = sum(t["points"] for t in detail["tasks"].values())
        cycles_detail.append(detail)
        print(f"[cycle {cycle}] wall={wall:.1f}s rows={detail['db_rows']} "
              f"monitor={detail['monitor']['time']:.1f}s "
              f"eval={eval_stats['time']:.1f}s/{eval_stats['points']}pts rss={detail['rss_mb']:.0f}MB",
              flush=True)

    bench_engine.dispose()
    engine.dispose()
    if not args.keep:
        drop_database(admin_url, args.dbname)

    walls = [c["wall_s"] for c in cycles_detail]
    return {
        "scale": args.devices,
        "cycles": args.cycles,
        "ifaces": args.ifaces,
        "latency_ms": [args.latency_min, args.latency_max],
        "concurrency": {"collect": sched.COLLECT_CONCURRENCY, "check": monitor_loop.CHECK_CONCURRENCY},
        "pool_size": pool_desc,
        "policy_job_wait_s": round(wait_s, 1),
        "device_mix": counts,
        "env": {
            "python": platform.python_version(),
            "cpu_cores": os.cpu_count(),
            "platform": platform.platform(),
            "db": "timescale/timescaledb:latest-pg15 (docker ops-postgres)",
        },
        "cycles_detail": cycles_detail,
        "summary": {
            "wall_avg_s": round(sum(walls) / len(walls), 2),
            "wall_max_s": round(max(walls), 2),
            "rows_total": sum(c["db_rows"] for c in cycles_detail),
            "rss_peak_mb": max(c["rss_mb"] for c in cycles_detail),
        },
    }


def main() -> None:
    args = parse_args()
    random.seed(args.seed)
    result = asyncio.run(run_bench(args))
    s = result["summary"]
    print(f"\n=== {result['scale']} 台 × {result['cycles']} 周期 ===")
    print(f"设备配比: {result['device_mix']}")
    print(f"周期墙钟: 平均 {s['wall_avg_s']}s / 最大 {s['wall_max_s']}s（采集间隔 60s）")
    print(f"metrics 写入总行数: {s['rows_total']}，RSS 峰值: {s['rss_peak_mb']:.0f}MB")
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"结果已写入 {args.json_path}")


if __name__ == "__main__":
    main()
