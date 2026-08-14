"""SSRF 防护：拨测目标地址封禁（未指定/链路本地含云元数据/组播/广播/回环）。
内网 NMS 拨测场景：目标本就是内网服务，因此**不封内网私网段**，只封危险保留段。
回环（127.0.0.0/8、::1）默认封禁，可用 OPS_SSRF_BLOCK_LOOPBACK=0 关闭——
测试套件大量用 127.0.0.1 起本地假服务做真拨测，conftest 已关闭；生产保持封禁。
纯函数 + 异步解析；零依赖。
"""
import asyncio
import ipaddress

from .config import get_settings

# 除回环外的封禁段：未指定、链路本地（169.254.x 含云元数据 169.254.169.254）、组播、广播
_BASE_BLOCKED = [
    "0.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "255.255.255.255/32",
    "::/128",
    "fe80::/10",
    "ff00::/8",
]
_LOOPBACK = ["127.0.0.0/8", "::1/128"]


def _blocked_nets() -> list:
    nets = [ipaddress.ip_network(n) for n in _BASE_BLOCKED]
    if get_settings().ssrf_block_loopback:
        nets += [ipaddress.ip_network(n) for n in _LOOPBACK]
    return nets


def is_blocked_ip(ip_str: str) -> bool:
    """字面 IP 是否在封禁范围（支持 IPv4/IPv6 与 IPv4-mapped IPv6）。非法输入返回 False。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _blocked_nets())


async def resolve_blocked(host: str) -> bool:
    """域名解析后任一条 A/AAAA 落在封禁段即 True；解析失败返回 False（由探针自身报不可达）。"""
    if is_blocked_ip(host):
        return True
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001 - 解析失败不判为封禁
        return False
    return any(is_blocked_ip(info[4][0]) for info in infos)


class SSRFBlockedError(Exception):
    """拨测目标命中封禁地址段。"""


async def ensure_not_blocked(host: str) -> None:
    """拨测前调用：目标（域名或字面 IP）解析后任一地址在封禁段即抛 SSRFBlockedError。"""
    if host and await resolve_blocked(host):
        raise SSRFBlockedError(f"拨测目标 {host} 命中封禁地址段")
