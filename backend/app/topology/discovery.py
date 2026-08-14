"""拓扑自动发现：SNMP 读 LLDP-MIB / 思科 CDP-MIB 邻居表，推算设备间链路。
邻居解析到库内设备：优先 IP（CDP 地址 / LLDP 管理地址），其次 sysName ↔ 设备名。
解析不到的邻居跳过不阻断（单点失败不阻塞整体）。
"""
import logging
from dataclasses import dataclass

from ..collectors import snmp
from ..models import Device

log = logging.getLogger(__name__)

OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"  # ifName（CDP 本端端口映射用）

# LLDP-MIB（标准）
OID_LLDP_LOC_PORT = "1.0.8802.1.1.2.1.3.7.1.3"     # lldpLocPortId（本端端口）
OID_LLDP_REM_CHASSIS = "1.0.8802.1.1.2.1.4.1.1.5"  # lldpRemChassisId
OID_LLDP_REM_PORT = "1.0.8802.1.1.2.1.4.1.1.7"     # lldpRemPortId
OID_LLDP_REM_SYSNAME = "1.0.8802.1.1.2.1.4.1.1.9"  # lldpRemSysName
OID_LLDP_REM_MGMT_ADDR = "1.0.8802.1.1.2.1.4.2.1.3"  # lldpRemManAddrIfSubtype（值含管理地址）

# Cisco CDP-MIB
OID_CDP_ADDRESS = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"   # cdpCacheAddress（邻居 IP，hex）
OID_CDP_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"  # cdpCacheDeviceId
OID_CDP_DEVICE_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"  # cdpCacheDevicePort
OID_CDP_IF_INDEX = "1.3.6.1.4.1.9.9.23.1.2.1.1.1"  # cdpCacheIfIndex（本端 ifIndex）


@dataclass
class Neighbor:
    """一条邻居记录：本端端口 ↔ 远端设备标识 + 远端端口。"""

    local_port: str
    remote_ip: str       # 可能为空
    remote_name: str     # 可能为空
    remote_port: str
    source: str          # lldp / cdp


def _suffix(oid: str, base: str) -> str:
    return oid[len(base) + 1:]


def _hex_to_ip(value: str) -> str:
    """CDP 地址是 hex 串（如 0A000001 或带空格），转点分 IP；转不了返回空。"""
    hexstr = value.replace(" ", "").replace(":", "")
    if len(hexstr) != 8:
        return ""
    try:
        return ".".join(str(int(hexstr[i:i + 2], 16)) for i in range(0, 8, 2))
    except ValueError:
        return ""


def _lldp_mgmt_map(rem_mgmt_addrs: dict[str, str]) -> dict[str, str]:
    """LLDP 管理地址表 → {邻居索引 timeMark.localPortNum.remIndex: ipv4}。
    OID 后缀结构（真机 H3C 实测）：timeMark.localPortNum.remIndex.addrSubtype.addrLen.addr...
    后面还跟着 ifSubtype/ifId/oid 等段——所以不能用「末 5 段」定位（旧实现因此永远取不到），
    addrSubtype=1 且 addrLen=4 才是 ipv4。同一邻居多个管理地址取第一个。"""
    result: dict[str, str] = {}
    for oid in rem_mgmt_addrs:
        parts = _suffix(oid, OID_LLDP_REM_MGMT_ADDR).split(".")
        if len(parts) >= 9 and parts[3] == "1" and parts[4] == "4":
            result.setdefault(".".join(parts[:3]), ".".join(parts[5:9]))
    return result


def parse_lldp(loc_ports, rem_ports, rem_sysnames, rem_mgmt_addrs) -> list[Neighbor]:
    """解析 LLDP 各列。远端表索引为 timeMark.localPortNum.remoteIndex，
    取中段 localPortNum 关联本端端口名；管理地址按邻居索引逐条关联。"""
    mgmt_map = _lldp_mgmt_map(rem_mgmt_addrs)
    neighbors = []
    for oid, remote_port in rem_ports.items():
        parts = _suffix(oid, OID_LLDP_REM_PORT).split(".")
        if len(parts) < 3:
            continue
        local_num = parts[1]
        local_port = ""
        for lo, lv in loc_ports.items():
            if _suffix(lo, OID_LLDP_LOC_PORT).split(".")[-1] == local_num or lv == local_num:
                local_port = lv
                break
        key = _suffix(oid, OID_LLDP_REM_PORT)
        sysname = next(
            (v for o, v in rem_sysnames.items() if _suffix(o, OID_LLDP_REM_SYSNAME) == key),
            "",
        )
        mgmt_ip = mgmt_map.get(key, "")
        neighbors.append(Neighbor(local_port, mgmt_ip, sysname, remote_port, "lldp"))
    return neighbors


def parse_cdp(addresses, device_ids, device_ports, if_names) -> list[Neighbor]:
    """解析 CDP 各列。索引为 ifIndex.entry，本端端口经 ifIndex→ifName 映射。"""
    neighbors = []
    for oid, dev_id in device_ids.items():
        idx = _suffix(oid, OID_CDP_DEVICE_ID)
        parts = idx.split(".")
        if len(parts) < 2:
            continue
        if_index = parts[0]
        local_port = if_names.get(f"{OID_IF_NAME}.{if_index}", "")
        remote_port = device_ports.get(f"{OID_CDP_DEVICE_PORT}.{idx}", "")
        raw_addr = addresses.get(f"{OID_CDP_ADDRESS}.{idx}", "")
        neighbors.append(Neighbor(local_port, _hex_to_ip(raw_addr), dev_id, remote_port, "cdp"))
    return neighbors


def resolve_device_id(neighbor: Neighbor, devices: list[Device]) -> int | None:
    """邻居解析到库内设备：IP 优先，其次设备名（sysName 常带域名后缀，做前缀匹配）。"""
    if neighbor.remote_ip:
        for d in devices:
            if d.ip == neighbor.remote_ip:
                return d.id
    if neighbor.remote_name:
        name = neighbor.remote_name.lower()
        for d in devices:
            dn = (d.name or "").lower()
            if dn and (dn == name or name.startswith(dn + ".")):
                return d.id
    return None


async def discover_device_neighbors(
    device: Device, payload: dict, fetch_walk=snmp.walk
) -> list[Neighbor]:
    """采集一台设备的 LLDP 与 CDP 邻居。单协议失败不影响另一协议。"""
    neighbors: list[Neighbor] = []

    try:  # LLDP
        rem_ports = await fetch_walk(device.ip, payload, OID_LLDP_REM_PORT)
        if rem_ports:
            loc_ports = await fetch_walk(device.ip, payload, OID_LLDP_LOC_PORT)
            sysnames = await fetch_walk(device.ip, payload, OID_LLDP_REM_SYSNAME)
            mgmt = await fetch_walk(device.ip, payload, OID_LLDP_REM_MGMT_ADDR)
            neighbors += parse_lldp(loc_ports, rem_ports, sysnames, mgmt)
    except Exception as e:  # noqa: BLE001
        log.debug("LLDP 发现失败 %s: %s", device.ip, e)

    try:  # CDP
        device_ids = await fetch_walk(device.ip, payload, OID_CDP_DEVICE_ID)
        if device_ids:
            addresses = await fetch_walk(device.ip, payload, OID_CDP_ADDRESS)
            dev_ports = await fetch_walk(device.ip, payload, OID_CDP_DEVICE_PORT)
            if_names = await fetch_walk(device.ip, payload, OID_IF_NAME)
            neighbors += parse_cdp(addresses, device_ids, dev_ports, if_names)
    except Exception as e:  # noqa: BLE001
        log.debug("CDP 发现失败 %s: %s", device.ip, e)

    return neighbors
