"""告警通知：SMTP 邮件、钉钉/企业微信机器人 Webhook。
发送失败静默记日志，绝不影响告警引擎主流程。
第 8 期：引擎/升级通过 schedule_alert 把发送移出评估关键路径（后台 task，失败静默）。
"""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

from ..core.database import SessionLocal
from ..models import NotifyConfig

log = logging.getLogger(__name__)

SEVERITY_CN = {"critical": "致命", "major": "严重", "warning": "警告", "info": "信息"}


def render_message(title: str, fields: dict) -> str:
    lines = [f"【{title}】"] + [f"{k}：{v}" for k, v in fields.items()]
    return "\n".join(lines)


def _send_smtp_sync(config: dict, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = config.get("from_addr", config.get("username", ""))
    recipients = config.get("to_addrs", [])
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP(config.get("host", ""), int(config.get("port", 25)), timeout=10) as smtp:
        if config.get("use_tls"):
            smtp.starttls()
        if config.get("username"):
            smtp.login(config["username"], config.get("password", ""))
        smtp.sendmail(msg["From"], recipients, msg.as_string())


async def _send_webhook(kind: str, config: dict, text: str) -> None:
    """钉钉与企业微信机器人 text 消息格式一致。"""
    import httpx

    url = config.get("webhook_url", "")
    if not url:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={"msgtype": "text", "text": {"content": text}})
        if resp.status_code != 200:
            log.warning("Webhook %s 返回 %s: %s", kind, resp.status_code, resp.text[:200])


async def send_alert(subject: str, body: str, channels: list[str] | None = None) -> None:
    """按渠道发送告警。channels=["*"] 表示全部启用渠道；为空则不发送。"""
    db = SessionLocal()
    try:
        configs = [c for c in db.query(NotifyConfig).filter(NotifyConfig.enabled.is_(True)).all()]
    finally:
        db.close()
    if not channels:
        return
    want_all = "*" in channels
    for cfg in configs:
        if not want_all and cfg.kind not in channels and cfg.name not in channels:
            continue
        data = cfg.get_config()
        try:
            if cfg.kind == "smtp":
                await asyncio.to_thread(_send_smtp_sync, data, subject, body)
            elif cfg.kind in ("dingtalk", "wecom"):
                await _send_webhook(cfg.kind, data, f"{subject}\n{body}")
        except Exception as e:  # noqa: BLE001 - 单个渠道失败不影响其他渠道
            log.warning("告警通知发送失败 %s(%s): %s", cfg.name, cfg.kind, e)


# ---------------------------------------------------------------- 后台发送（评估关键路径解耦）

# 已入队未发完的通知任务（防止 task 被 GC，并供测试等待）
_pending: set[asyncio.Task] = set()


async def _send_quiet(subject: str, body: str, channels: list[str] | None) -> None:
    """后台发送包装：任何异常静默（含渠道配置查询失败），绝不影响评估主流程。"""
    try:
        await send_alert(subject, body, channels)
    except Exception:  # noqa: BLE001
        log.warning("告警通知后台发送失败：%s", subject, exc_info=True)


def schedule_alert(subject: str, body: str, channels: list[str] | None = None) -> None:
    """把通知发送移出调用方关键路径：建后台 task 立即返回，失败静默。"""
    try:
        task = asyncio.create_task(_send_quiet(subject, body, channels))
    except RuntimeError:  # 无运行中的事件循环（不应发生），静默丢弃
        log.warning("无事件循环，丢弃告警通知：%s", subject)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def wait_pending(timeout: float = 5.0) -> None:
    """等待已入队的通知全部发完（测试断言用；超时返回不抛异常）。"""
    pending = [t for t in _pending if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
