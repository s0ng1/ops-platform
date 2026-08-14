"""异步 ping：解析 IP 段 + 协程并发扫描（移植自 ip-monitor/scanner.py，改为 asyncio）。"""
import asyncio
import ipaddress
import re

MAX_IPS = 4096          # 单次扫描上限，防止误输超大网段卡死
PING_CONCURRENCY = 100  # 并发协程数
PING_TIMEOUT_S = 1


def parse_ranges(text: str):
    """解析 IP 段，支持三种写法逗号/换行分隔混用：
    192.168.1.1-192.168.1.254 / 192.168.1.5 / 192.168.1.0/24
    返回 (ips, errors)，ips 为去重排序后的字符串列表。
    """
    ips = set()
    errors = []
    tokens = [t.strip() for t in re.split(r"[,，;；\n]+", text) if t.strip()]
    for token in tokens:
        try:
            if "-" in token:
                parts = [p.strip() for p in token.split("-")]
                if len(parts) != 2:
                    raise ValueError("范围写法应为 起始IP-结束IP")
                start = ipaddress.ip_address(parts[0])
                end = ipaddress.ip_address(parts[1])
                if start.version != end.version:
                    raise ValueError("起始和结束 IP 版本不一致")
                if int(end) < int(start):
                    raise ValueError("结束 IP 小于起始 IP")
                count = int(end) - int(start) + 1
                if count > MAX_IPS:
                    raise ValueError(f"范围内含 {count} 个 IP，超过上限 {MAX_IPS}")
                for i in range(int(start), int(end) + 1):
                    ips.add(str(ipaddress.ip_address(i)))
            elif "/" in token:
                net = ipaddress.ip_network(token, strict=False)
                hosts = list(net.hosts()) or [net.network_address]
                if len(hosts) > MAX_IPS:
                    raise ValueError(f"网段内含 {len(hosts)} 个 IP，超过上限 {MAX_IPS}")
                for h in hosts:
                    ips.add(str(h))
            else:
                ips.add(str(ipaddress.ip_address(token)))
        except ValueError as e:
            errors.append(f"「{token}」无效：{e}")
    return sorted(ips, key=lambda s: tuple(int(p) for p in s.split("."))), errors


async def ping(ip: str, timeout: int = PING_TIMEOUT_S) -> tuple[bool, int | None]:
    """ping 单个 IP，返回 (online, latency_ms|None)。失败一律回 (False, None)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(max(1, timeout)), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
    except (asyncio.TimeoutError, OSError):
        return False, None
    if proc.returncode != 0:
        return False, None
    out = stdout.decode(errors="ignore")
    m = re.search(r"time[=<](\d+(?:\.\d+)?)\s*ms", out, re.IGNORECASE)
    latency = int(float(m.group(1))) if m else 0
    return True, latency


async def ping_sweep(ips: list[str], on_progress=None) -> dict[str, tuple[bool, int | None]]:
    """协程池并发 ping，返回 {ip: (online, latency)}。on_progress(done, total) 可选。"""
    sem = asyncio.Semaphore(PING_CONCURRENCY)
    results: dict[str, tuple[bool, int | None]] = {}
    done = 0
    total = len(ips)

    async def worker(ip: str):
        nonlocal done
        async with sem:
            results[ip] = await ping(ip)
            done += 1
            if on_progress:
                on_progress(done, total)

    await asyncio.gather(*(worker(ip) for ip in ips))
    return results
