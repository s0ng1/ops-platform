"""告警升级：firing 且超时未确认（未 ack）的事件 severity 升一级并重发通知。
同一事件只升一次（escalated 标记），升到 critical 封顶；escalate_minutes=0 不升级。
"""
import asyncio
import logging
from datetime import datetime

from ..core.database import SessionLocal
from ..models import AlertEvent, AlertRule, Device
from ..models.alert import SEVERITIES
from . import notify

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # 秒


async def check_escalations(now: datetime | None = None) -> int:
    """执行一轮升级检查，返回升级的事件数。整体异常自吞。"""
    now = now or datetime.now()
    try:
        # 同步 DB 块整体丢线程池；事务在发通知前了结，通知期间不持有连接
        pending = await asyncio.to_thread(_apply_escalations, now)
    except Exception:  # noqa: BLE001
        log.exception("告警升级检查异常")
        return 0
    # 提交后再入队通知：后台发送，通知失败不影响升级记录，也不阻塞下一轮检查
    for item in pending:
        _schedule_notification(item, now)
    return len(pending)


def _apply_escalations(now: datetime) -> list[dict]:
    """查出超时未 ack 的 firing 事件并升级，提交后返回待通知的纯数据列表。供 to_thread 调用。"""
    db = SessionLocal()
    try:
        rules = {
            r.id: r
            for r in db.query(AlertRule).filter(AlertRule.escalate_minutes > 0).all()
        }
        if not rules:
            return []
        events = (
            db.query(AlertEvent)
            .filter(
                AlertEvent.status == "firing",
                AlertEvent.ack_by == "",
                AlertEvent.escalated.is_(False),
                AlertEvent.rule_id.in_(list(rules)),
            )
            .all()
        )
        devices = {d.id: d for d in db.query(Device).all()}
        pending = []  # 通知所需的纯数据字典（会话关闭后仍可用）
        for ev in events:
            rule = rules.get(ev.rule_id)
            if rule is None:
                continue
            if (now - ev.fired_at).total_seconds() < rule.escalate_minutes * 60:
                continue
            if ev.severity not in SEVERITIES:
                continue
            idx = SEVERITIES.index(ev.severity)
            if idx == 0:
                continue  # 已是 critical，封顶
            old = ev.severity
            ev.severity = SEVERITIES[idx - 1]
            ev.escalated = True
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} 超时未确认，等级由 {old} 升级为 {ev.severity}"
            ev.note = f"{ev.note}\n{line}" if ev.note else line
            device = devices.get(ev.device_id)
            pending.append(
                {
                    "event_id": ev.id,
                    "rule_name": rule.name,
                    "notify": rule.notify or ["*"],
                    "device": f"{device.name}({device.ip})" if device else f"设备#{ev.device_id}",
                    "metric": ev.metric,
                    "labels": ev.labels or {},
                    "severity": ev.severity,
                    "old": old,
                    "value": ev.value,
                }
            )
        db.commit()
        return pending
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()


def _schedule_notification(item: dict, now: datetime) -> None:
    """升级重发通知入队后台发送（走该规则的 notify 渠道），失败静默。"""
    try:
        labels_str = " ".join(f"{k}={v}" for k, v in item["labels"].items())
        subject = (
            f"[{notify.SEVERITY_CN.get(item['severity'], item['severity'])}]"
            f" {item['device']} {item['rule_name']}（升级）"
        )
        body = notify.render_message(
            "告警升级",
            {
                "规则": item["rule_name"],
                "设备": item["device"],
                "指标": f"{item['metric']} {labels_str}".strip(),
                "等级": f"{item['old']} → {item['severity']}（超时未确认）",
                "当前值": round(item["value"], 2),
                "时间": now.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        notify.schedule_alert(subject, body, item["notify"])
    except Exception:  # noqa: BLE001
        log.exception("升级通知入队失败 event_id=%s", item["event_id"])


async def escalation_loop(stop_event: asyncio.Event) -> None:
    """常驻循环，每 60 秒检查一轮，直到 stop_event 置位。"""
    while not stop_event.is_set():
        try:
            await check_escalations()
        except Exception:  # noqa: BLE001
            log.exception("告警升级循环异常")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass
