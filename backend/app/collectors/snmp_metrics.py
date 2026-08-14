"""网络/安全设备 SNMP 指标采集：接口流量/状态 + 设备级 CPU/内存。
厂商适配按 sysObjectID 前缀选择 OID，未识别的厂商跳过设备级指标（接口指标用标准 MIB 不受影响）。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import Device
from . import snmp
from .rate import RateCalculator

log = logging.getLogger(__name__)

# IF-MIB / ifXTable 标准列
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"        # ifName
OID_IF_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"      # ifHCInOctets（64 位计数器）
OID_IF_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"    # ifHCOutOctets
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15" # ifHighSpeed（Mbps）
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"    # ifOperStatus（1=up）

# 设备级指标：sysObjectID 前缀 -> 采集方式
# walk_avg：walk 后对全部实例取平均（如多板卡取均值）
# walk_max：walk 后对全部实例取最大（框式设备最忙板卡，H3C 实测空槽返回 0，取均值会被稀释）
# pool_pct：used/(used+free) 逐内存池计算后取最大（最紧张池）
VENDOR_METRICS = {
    "1.3.6.1.4.1.9": {  # Cisco
        "cpu_usage": ("walk_avg", "1.3.6.1.4.1.9.9.109.1.1.1.1.8"),
        "mem_usage": (
            "pool_pct",
            ("1.3.6.1.4.1.9.9.48.1.1.1.5", "1.3.6.1.4.1.9.9.48.1.1.1.6"),
        ),
    },
    "1.3.6.1.4.1.2011": {  # 华为（hwEntityStatTable）
        "cpu_usage": ("walk_avg", "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5"),
        "mem_usage": ("walk_avg", "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7"),
    },
    "1.3.6.1.4.1.25506": {  # H3C（hh3cEntityExtTable，框式设备按板卡实例，取最大=最忙板卡）
        "cpu_usage": ("walk_max", "1.3.6.1.4.1.25506.2.6.1.1.1.1.6"),
        "mem_usage": ("walk_max", "1.3.6.1.4.1.25506.2.6.1.1.1.1.8"),
    },
    # 锐捷/防火墙等私有 OID 按现场设备型号补充
}


@dataclass
class MetricPoint:
    device_id: int
    metric: str
    value: float
    labels: dict = field(default_factory=dict)
    time: datetime | None = None


def _walk_avg(values: dict[str, str]) -> float | None:
    nums = []
    for v in values.values():
        try:
            nums.append(float(v))
        except (ValueError, TypeError):
            continue
    return sum(nums) / len(nums) if nums else None


def _walk_max(values: dict[str, str]) -> float | None:
    """多实例取最大（框式设备最忙板卡，空槽位返回 0 不稀释结果）。"""
    nums = []
    for v in values.values():
        try:
            nums.append(float(v))
        except (ValueError, TypeError):
            continue
    return max(nums) if nums else None


def _pool_pct(used: dict[str, str], free: dict[str, str]) -> float | None:
    """used/(used+free)，OID 实例后缀对齐，取最紧张的池。"""
    pcts = []
    for oid, u in used.items():
        suffix = oid.split(".")[-1]
        f = next((v for o, v in free.items() if o.endswith("." + suffix)), None)
        try:
            u_n, f_n = float(u), float(f)
        except (ValueError, TypeError):
            continue
        if u_n + f_n > 0:
            pcts.append(u_n / (u_n + f_n) * 100)
    return max(pcts) if pcts else None


async def collect_device_metrics(
    device: Device,
    payload: dict,
    rate_calc: RateCalculator,
    fetch_walk=snmp.walk,
    fetch_get=snmp.get_multi,
) -> list[MetricPoint]:
    """采集一台 SNMP 设备的全部指标。fetch_* 可注入假数据便于测试。
    单项失败仅记日志，不抛出（单点失败不阻塞整体）。
    """
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = datetime.now(timezone.utc).timestamp()
    host, did = device.ip, device.id

    # ---- 接口指标 ----
    try:
        names = await fetch_walk(host, payload, OID_IF_NAME)
        if names:
            ins = await fetch_walk(host, payload, OID_IF_HC_IN)
            outs = await fetch_walk(host, payload, OID_IF_HC_OUT)
            speeds = await fetch_walk(host, payload, OID_IF_HIGH_SPEED)
            statuses = await fetch_walk(host, payload, OID_IF_OPER_STATUS)

            for oid, name in names.items():
                idx = oid[len(OID_IF_NAME) + 1:]
                labels = {"if": name or idx}
                in_raw = ins.get(f"{OID_IF_HC_IN}.{idx}", "")
                out_raw = outs.get(f"{OID_IF_HC_OUT}.{idx}", "")
                speed = speeds.get(f"{OID_IF_HIGH_SPEED}.{idx}", "")
                status = statuses.get(f"{OID_IF_OPER_STATUS}.{idx}", "")

                if status:
                    points.append(MetricPoint(did, "if_status", 1.0 if status == "1" else 0.0, labels, now))
                speed_bps = None
                try:
                    speed_bps = float(speed) * 1_000_000 if speed else None
                except ValueError:
                    pass
                for direction, raw, table_oid in (("in", in_raw, OID_IF_HC_IN), ("out", out_raw, OID_IF_HC_OUT)):
                    try:
                        counter = int(raw)
                    except (ValueError, TypeError):
                        continue
                    bps = rate_calc.rate((did, idx, direction), ts, counter, bits=64)
                    if bps is None:
                        continue
                    bps *= 8  # 字节计数器 -> bit/s
                    points.append(MetricPoint(did, f"if_{direction}_bps", bps, labels, now))
                    if speed_bps:
                        util = min(bps / speed_bps * 100, 100.0)
                        points.append(MetricPoint(did, f"if_{direction}_util", util, labels, now))
    except Exception as e:  # noqa: BLE001
        log.debug("接口指标采集失败 %s: %s", host, e)

    # ---- 设备级 CPU/内存（按 sysObjectID 厂商前缀适配）----
    try:
        prefix = next((p for p in VENDOR_METRICS if device.sys_object_id.startswith(p)), None)
        if prefix:
            for metric, spec in VENDOR_METRICS[prefix].items():
                try:
                    kind, oids = spec
                    if kind == "walk_avg":
                        value = _walk_avg(await fetch_walk(host, payload, oids))
                    elif kind == "walk_max":
                        value = _walk_max(await fetch_walk(host, payload, oids))
                    else:  # pool_pct
                        used = await fetch_walk(host, payload, oids[0])
                        free = await fetch_walk(host, payload, oids[1])
                        value = _pool_pct(used, free)
                    if value is not None:
                        points.append(MetricPoint(did, metric, round(value, 2), {}, now))
                except Exception as e:  # noqa: BLE001
                    log.debug("设备级指标 %s 采集失败 %s: %s", metric, host, e)
    except Exception as e:  # noqa: BLE001
        log.debug("设备级指标采集失败 %s: %s", host, e)

    return points
