"""告警引擎：对采集点逐条匹配规则，连续 N 周期越限触发（去抖），恢复自动关闭。
去抖计数存进程内存（重启后重新计数，已开事件不受影响）。
baseline_dev 规则：基线按 (设备,指标) 去重后每批查一次内存缓存（alerting/baseline.py）；
_fire 时抓设备指标快照存事件（按设备去重，失败静默存 None）。
第 8 期：_fire 的通知发送经 notify.schedule_alert 移出评估关键路径（后台 task，失败静默）。
"""
import asyncio
import json
import logging
import operator
from datetime import datetime

from sqlalchemy import func

from ..collectors.snmp_metrics import MetricPoint
from ..core.database import SessionLocal
from ..models import AlertEvent, AlertRule, Device, Metric, SilenceWindow
from ..models.metric import utcnow
from . import baseline, notify

log = logging.getLogger(__name__)

_OPS = {
    ">": operator.gt, ">=": operator.ge, "<": operator.lt,
    "<=": operator.le, "==": operator.eq, "!=": operator.ne,
}

# 去抖计数：{(rule_id, device_id, labels_key): 连续越限次数}
_breach_counts: dict[tuple, int] = {}


def labels_key(labels: dict) -> str:
    return json.dumps(labels, sort_keys=True, ensure_ascii=False)


def _match_selector(rule: AlertRule, device: Device) -> bool:
    if rule.device_id and rule.device_id != device.id:
        return False
    if rule.device_type and rule.device_type != device.type:
        return False
    if rule.group_name and rule.group_name != device.group_name:
        return False
    return True


def _match_labels(rule: AlertRule, labels: dict) -> bool:
    return all(labels.get(k) == v for k, v in (rule.labels_filter or {}).items())


def _naive(dt: datetime) -> datetime:
    """统一为本地朴素时间，避免带时区时间比较报错。"""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _active_silence(db, device: Device, now: datetime) -> SilenceWindow | None:
    """返回当前命中设备的活跃静默窗口，无则 None。选择器语义同规则。"""
    windows = db.query(SilenceWindow).filter(SilenceWindow.enabled.is_(True)).all()
    for w in windows:
        if _naive(w.start_at) <= now <= _naive(w.end_at) and _match_selector(w, device):
            return w
    return None


def _load_if_up_history(db, rules: list[AlertRule], points: list[MetricPoint]) -> set | None:
    """查询本批 if_status 点中「历史上曾 up（value=1）」的 (device_id, labels_key) 集合。
    无 if_status 规则或本批无 if_status 点时返回 None（无需判定）；
    查询异常静默放行（返回 None = 不拦截，宁可误报不丢报），记 warning。
    """
    if not any(r.metric == "if_status" for r in rules):
        return None
    keys = {(p.device_id, labels_key(p.labels)) for p in points if p.metric == "if_status"}
    if not keys:
        return None
    try:
        device_ids = {d for d, _ in keys}
        rows = (
            db.query(Metric.device_id, Metric.labels)
            .filter(
                Metric.metric == "if_status",
                Metric.value == 1,
                Metric.device_id.in_(device_ids),
            )
            .distinct()
            .all()
        )
        return {(did, labels_key(labels or {})) for did, labels in rows} & keys
    except Exception:  # noqa: BLE001
        log.warning("接口 up 历史查询失败，本次放行 if_status 告警", exc_info=True)
        return None


def _capture_snapshot(device_id: int) -> dict | None:
    """抓设备当前全部指标最新值（口径同 metrics/latest API：每 metric+labels 最新一条）。
    查询失败静默返回 None（不阻塞告警）。供 to_thread 调用（自带独立会话）。
    """
    db = SessionLocal()
    try:
        sub = (
            db.query(Metric.metric, Metric.labels, func.max(Metric.time).label("mt"))
            .filter(Metric.device_id == device_id)
            .group_by(Metric.metric, Metric.labels)
            .subquery()
        )
        rows = (
            db.query(Metric)
            .join(
                sub,
                (Metric.device_id == device_id)
                & (Metric.metric == sub.c.metric)
                & (Metric.labels == sub.c.labels)
                & (Metric.time == sub.c.mt),
            )
            .all()
        )
        return {
            "items": [
                {"metric": r.metric, "labels": r.labels, "value": r.value,
                 "time": r.time.isoformat()}
                for r in rows
            ]
        }
    except Exception:  # noqa: BLE001
        log.warning("告警快照抓取失败 device=%s，本次存空", device_id, exc_info=True)
        return None
    finally:
        db.close()


async def evaluate_points(points: list[MetricPoint]) -> None:
    """对一批采集点评估全部启用规则。整体异常自吞，不阻塞采集主流程。"""
    if not points:
        return
    db = SessionLocal()
    try:
        rules = db.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
        if not rules:
            return
        devices = {d.id: d for d in db.query(Device).all()}
        # 本批设备的未恢复事件一次性预加载成 {(rule_id, device_id, labels_key): 事件}，
        # _fire/_resolve 查内存字典，避免逐点 SELECT（大批量时是主要开销）
        device_ids = {p.device_id for p in points}
        open_events = {
            (ev.rule_id, ev.device_id, ev.labels_key): ev
            for ev in db.query(AlertEvent)
            .filter(
                AlertEvent.device_id.in_(device_ids),
                AlertEvent.status == "firing",
            )
            .all()
        }
        # 接口 down 降噪：本批 if_status 点中曾 up 过的 (device_id, labels_key)，一次性查询当批复用
        if_up_history = _load_if_up_history(db, rules, points)
        # 动态基线：baseline_dev 规则涉及的 (设备,指标) 去重后一次查完，本批内存缓存；
        # 同步查询走 to_thread 不阻塞事件循环，异常在 baseline 模块内静默跳过
        baselines: dict = {}
        baseline_metrics = {r.metric for r in rules if r.op == "baseline_dev"}
        if baseline_metrics:
            pairs = {(p.device_id, p.metric) for p in points if p.metric in baseline_metrics}
            if pairs:
                try:
                    baselines = await asyncio.to_thread(baseline.load_baselines, pairs, utcnow())
                except Exception:  # noqa: BLE001 - 基线整体失败不阻塞评估，按无基线（不触发）处理
                    log.warning("基线批量查询失败，本批 baseline_dev 规则不触发", exc_info=True)
                    baselines = {}
        # _fire 的设备指标快照缓存：{device_id: snapshot 或 None}，同设备本批只抓一次
        snapshots: dict[int, dict | None] = {}
        for p in points:
            device = devices.get(p.device_id)
            if device is None:
                continue
            for rule in rules:
                if rule.metric != p.metric:
                    continue
                if not _match_selector(rule, device) or not _match_labels(rule, p.labels):
                    continue
                if rule.op == "baseline_dev":
                    bl = baselines.get((p.device_id, p.metric))
                    # 无基线（查询失败/无历史）或样本不足均不触发
                    breach = bl is not None and baseline.is_breach(p.value, rule.threshold, bl)
                else:
                    breach = _OPS[rule.op](p.value, rule.threshold)
                key = (rule.id, p.device_id, labels_key(p.labels))
                if breach:
                    # 接口 down 只告历史上曾 up 过的口；从未 up 的口不告警也不计去抖
                    # （if_up_history 为 None 表示历史查询失败，静默放行宁可误报）
                    if (
                        rule.metric == "if_status"
                        and if_up_history is not None
                        and (p.device_id, key[2]) not in if_up_history
                    ):
                        continue
                    _breach_counts[key] = _breach_counts.get(key, 0) + 1
                    if _breach_counts[key] >= rule.duration_cycles:
                        await _fire(db, rule, device, p, open_events, snapshots)
                else:
                    _breach_counts.pop(key, None)
                    _resolve(rule, p, open_events)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("告警评估异常")
    finally:
        db.close()


async def _fire(
    db, rule: AlertRule, device: Device, p: MetricPoint,
    open_events: dict, snapshots: dict[int, dict | None],
) -> None:
    """触发事件；同 (规则,设备,labels) 已有未恢复事件则跳过（不重复告警）。"""
    lkey = labels_key(p.labels)
    if (rule.id, p.device_id, lkey) in open_events:
        return
    # 触发时刻的设备指标快照：同设备本批只抓一次（to_thread），失败静默存 None
    if p.device_id not in snapshots:
        try:
            snapshots[p.device_id] = await asyncio.to_thread(_capture_snapshot, p.device_id)
        except Exception:  # noqa: BLE001 - 双保险，快照绝不影响告警
            log.warning("告警快照抓取异常 device=%s，本次存空", p.device_id, exc_info=True)
            snapshots[p.device_id] = None
    silenced = _active_silence(db, device, datetime.now()) is not None
    event = AlertEvent(
        rule_id=rule.id,
        rule_name=rule.name,
        device_id=p.device_id,
        metric=p.metric,
        labels=p.labels,
        labels_key=lkey,
        severity=rule.severity,
        value=p.value,
        silenced=silenced,
        snapshot=snapshots[p.device_id],
    )
    db.add(event)
    db.flush()  # 先落库拿 id，通知失败也不丢事件
    # 同步进预加载字典：同批后续相同 key 的点不会重复触发
    open_events[(rule.id, p.device_id, lkey)] = event
    # WS 推送新告警（前端顶栏/告警中心实时刷新）
    from ..core.broadcast import broadcaster

    await broadcaster.broadcast(
        {
            "type": "alert",
            "event_id": event.id,
            "rule_name": rule.name,
            "device_id": p.device_id,
            "device_name": device.name,
            "severity": rule.severity,
            "metric": p.metric,
            "value": round(p.value, 2),
        }
    )
    if silenced:
        # 静默窗口内：事件与 WS 广播照常，仅跳过外部通知
        log.info("告警命中静默窗口，跳过外部通知 rule=%s device=%s", rule.name, device.name)
        return
    labels_str = " ".join(f"{k}={v}" for k, v in p.labels.items())
    subject = f"[{notify.SEVERITY_CN.get(rule.severity, rule.severity)}] {device.name}({device.ip}) {rule.name}"
    cond = (f"偏离基线 > {rule.threshold}σ" if rule.op == "baseline_dev"
            else f"{rule.op} {rule.threshold}")
    body = notify.render_message(
        "告警触发",
        {
            "规则": rule.name,
            "设备": f"{device.name}({device.ip})",
            "指标": f"{p.metric} {labels_str}".strip(),
            "条件": f"{cond}（连续 {rule.duration_cycles} 周期）",
            "当前值": round(p.value, 2),
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    # 通知发送移出评估关键路径：后台 task 发送，失败静默不影响事件
    notify.schedule_alert(subject, body, rule.notify or ["*"])


def _resolve(rule: AlertRule, p: MetricPoint, open_events: dict) -> None:
    """指标恢复正常：关闭对应未恢复事件。"""
    open_event = open_events.pop((rule.id, p.device_id, labels_key(p.labels)), None)
    if open_event is not None:
        open_event.status = "resolved"
        open_event.resolved_at = datetime.now()


def reset_counters() -> None:
    """清空去抖计数（测试用）。"""
    _breach_counts.clear()
