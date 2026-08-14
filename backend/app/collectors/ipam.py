"""轻量 IPAM 采集：交换机 ARP 表（IP↔MAC）+ MAC 地址表（MAC→接入端口）回写台账。

本期取舍（保持简单，注释备查）：
- 只入有 IP 的记录（arp / ping 来源）。dot1dTpFdbTable 里无 IP 的纯 MAC 终端不入库
  （ip 是全表唯一键，纯 MAC 记录无处安放），MAC 表数据仅用于给已知 IP 的记录补
  device_id / if_name（接入端口）。现场确需纯 MAC 台账时再扩展。
- hostname 不做反向解析，留空；可通过 API 手工维护作备注。

新终端检测：upsert 插入新记录（非白名单）时产 new_terminal 指标点，交告警引擎
（内置「新终端接入」规则，info 级，同「配置变更」「日志事件」模式）。已有记录更新
（含 MAC 变化）不告警——本期只告新 IP。全部失败静默回退，单点失败不阻塞调度主流程。
"""
import asyncio
import ipaddress
import logging
from datetime import datetime, timezone

from ..core.database import SessionLocal
from ..models import Device, IpInventory
from . import snmp
from .snmp_metrics import MetricPoint

log = logging.getLogger(__name__)

# ipNetToMediaTable（RFC 1213 老 ARP 表，设备支持面最广）：列 .3=MAC，
# OID 后缀为 ifIndex.IP——walk 单列即可同时拿到 IP、MAC，无需再 walk .2/.4 列
# ipNetToMediaTable 列号：.1=ifIndex、.2=PhysAddress(MAC)、.3=NetAddress(IP)、.4=Type。
# 曾误用 .3（拿到的是 IP 文本，真机解析全丢），真机 H3C 实测确认 .2 才是 MAC。
OID_ARP_MAC = "1.3.6.1.2.1.4.22.1.2"      # ipNetToMediaPhysAddress
# BRIDGE-MIB dot1dTpFdbTable：列 .2=端口 ifIndex，OID 后缀为 MAC 的 6 段十进制
OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"   # dot1dTpFdbPort
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"    # ifName（ifIndex → 端口名）

_HEX = frozenset("0123456789abcdef")


def normalize_mac(text: str) -> str | None:
    """MAC 归一化为小写冒号格式（aa:bb:cc:dd:ee:ff），无法识别返回 None。支持：
    0x 十六进制（pysnmp OctetString 原样）、冒号/横杠分节、Cisco 点分、
    6 段十进制（dot1dTpFdbTable OID 后缀）、裸 12 位十六进制。"""
    s = (text or "").strip().lower()
    if not s:
        return None
    if s.startswith("0x"):
        hex_part = s[2:]
        if len(hex_part) == 12 and all(c in _HEX for c in hex_part):
            return ":".join(hex_part[i:i + 2] for i in range(0, 12, 2))
        return None
    for sep in (":", "-"):
        if sep in s:
            parts = s.split(sep)
            if len(parts) == 6 and all(0 < len(p) <= 2 and all(c in _HEX for c in p) for p in parts):
                return ":".join(p.zfill(2) for p in parts)
            return None
    if "." in s:
        parts = s.split(".")
        # Cisco 点分十六进制 aabb.ccdd.eeff
        if len(parts) == 3 and all(len(p) == 4 and all(c in _HEX for c in p) for p in parts):
            hex_part = "".join(parts)
            return ":".join(hex_part[i:i + 2] for i in range(0, 12, 2))
        # 6 段十进制（dot1dTpFdbTable OID 后缀的 MAC）
        if len(parts) == 6:
            try:
                octets = [int(p) for p in parts]
            except ValueError:
                return None
            if all(0 <= o <= 255 for o in octets):
                return ":".join(f"{o:02x}" for o in octets)
        return None
    # 裸 12 位十六进制
    if len(s) == 12 and all(c in _HEX for c in s):
        return ":".join(s[i:i + 2] for i in range(0, 12, 2))
    return None


def parse_arp_table(mac_column: dict[str, str]) -> list[dict]:
    """解析 ipNetToMediaPhysAddress walk 结果：OID 后缀 = ifIndex.IP。
    返回 [{"ip", "mac"}]，按 IP 去重，跳过无效行。"""
    entries: dict[str, dict] = {}
    for oid, raw_mac in mac_column.items():
        suffix = oid[len(OID_ARP_MAC):].lstrip(".")
        _, _, ip = suffix.partition(".")
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        mac = normalize_mac(raw_mac)
        if mac is None:
            continue
        entries[ip] = {"ip": ip, "mac": mac}
    return list(entries.values())


def parse_fdb_table(port_column: dict[str, str]) -> dict[str, int]:
    """解析 dot1dTpFdbPort walk 结果：OID 后缀 = MAC（6 段十进制），值 = 端口 ifIndex。
    返回 {mac: port_ifindex}。MAC 直接从 OID 后缀取，不依赖值的文本编码。"""
    result: dict[str, int] = {}
    for oid, raw_port in port_column.items():
        suffix = oid[len(OID_FDB_PORT):].lstrip(".")
        mac = normalize_mac(suffix)
        try:
            port = int(raw_port)
        except (ValueError, TypeError):
            continue
        if mac is not None and port > 0:
            result[mac] = port
    return result


def parse_if_names(name_column: dict[str, str]) -> dict[int, str]:
    """ifName walk 结果 → {ifindex: 端口名}。"""
    result: dict[int, str] = {}
    for oid, name in name_column.items():
        try:
            idx = int(oid[len(OID_IF_NAME):].lstrip("."))
        except ValueError:
            continue
        if name:
            result[idx] = name
    return result


def new_terminal_point(device_id: int, entry: dict, source: str) -> MetricPoint:
    """构造 new_terminal 指标点（1=发现新终端），labels 带 ip/mac/来源/接入端口。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    labels = {"ip": entry["ip"], "mac": entry.get("mac") or "", "source": source}
    if entry.get("if_name"):
        labels["if"] = entry["if_name"]
    return MetricPoint(device_id, "new_terminal", 1.0, labels, now)


def _upsert_entries(entries: list[dict], device_id: int | None, source: str) -> list[MetricPoint]:
    """台账 upsert（同步 DB，供 to_thread 调用）：已存在更新 mac/接入设备/端口/last_seen，
    不存在插入（first_seen=last_seen=now）。返回新终端指标点（仅新插入且非白名单）。
    告警事件必须挂设备：arp 来源挂接入交换机（device_id 入参），ping 来源挂同 IP 已入库
    设备，两者都没有则只入台账不出点（同「日志事件」未入库来源不告警的语义）。
    """
    if not entries:
        return []
    db = SessionLocal()
    try:
        now = datetime.now()
        ips = [e["ip"] for e in entries]
        existing = {
            r.ip: r for r in db.query(IpInventory).filter(IpInventory.ip.in_(ips)).all()
        }
        points: list[MetricPoint] = []
        for e in entries:
            row = existing.get(e["ip"])
            if row is not None:
                if e.get("mac"):
                    row.mac = e["mac"]
                if e.get("if_name"):
                    row.if_name = e["if_name"]
                if device_id is not None:
                    row.device_id = device_id
                # arp 来源信息更全（带 MAC），ping 只证明存活，不覆盖 arp 来源标记
                if not (row.source == "arp" and source == "ping"):
                    row.source = source
                row.last_seen = now
                continue
            row = IpInventory(
                ip=e["ip"],
                mac=e.get("mac"),
                device_id=device_id,
                if_name=e.get("if_name"),
                source=source,
                first_seen=now,
                last_seen=now,
            )
            db.add(row)
            if not row.whitelisted:
                event_device_id = device_id
                if event_device_id is None:
                    dev = db.query(Device).filter(Device.ip == e["ip"]).first()
                    event_device_id = dev.id if dev else None
                if event_device_id is not None:
                    points.append(new_terminal_point(event_device_id, e, source))
        db.commit()
        return points
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("IPAM 台账入库失败")
        return []
    finally:
        db.close()


async def collect_ipam(
    device: Device,
    payload: dict,
    fetch_walk=snmp.walk,
) -> list[MetricPoint]:
    """调度器入口：walk 一台交换机的 ARP/MAC 表回写台账，返回新终端指标点。
    fetch_walk 可注入假数据便于测试。"""
    host = device.ip
    try:
        arp_raw = await fetch_walk(host, payload, OID_ARP_MAC)
    except Exception as e:  # noqa: BLE001 - ARP 表拿不到本轮整体跳过
        log.debug("IPAM ARP 表采集失败 %s: %s", host, e)
        return []
    entries = parse_arp_table(arp_raw)
    if not entries:
        return []
    # MAC 地址表 + ifName 补接入端口（失败静默，仅影响 if_name 字段完整性）
    try:
        mac_port = parse_fdb_table(await fetch_walk(host, payload, OID_FDB_PORT))
        if_names = parse_if_names(await fetch_walk(host, payload, OID_IF_NAME))
        for e in entries:
            port_idx = mac_port.get(e["mac"])
            if port_idx is not None:
                e["if_name"] = if_names.get(port_idx)
    except Exception as e:  # noqa: BLE001
        log.debug("IPAM MAC 表采集失败 %s: %s", host, e)
    return await asyncio.to_thread(_upsert_entries, entries, device.id, "arp")


async def upsert_scan_results(ips: list[str]) -> list[MetricPoint]:
    """IP 段扫描存活结果回写台账（source=ping），返回新终端指标点。
    供自动发现扫描挂钩调用；异常静默（内部 _upsert_entries 已自吞）。"""
    entries = [{"ip": ip} for ip in ips]
    return await asyncio.to_thread(_upsert_entries, entries, None, "ping")
