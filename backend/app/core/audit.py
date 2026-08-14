"""操作审计：record() 自建会话写 audit_logs，异常静默吞掉，绝不阻塞业务。
同步函数；异步上下文调用方可用 asyncio.to_thread 包裹。
"""
import logging

from .database import SessionLocal
from ..models import AuditLog

log = logging.getLogger(__name__)


def record(username: str, action: str, target: str = "", detail: str = "", ip: str = "") -> None:
    """写一条审计日志。任何失败只记日志不抛出。"""
    try:
        db = SessionLocal()
        try:
            db.add(
                AuditLog(
                    username=username or "",
                    action=action,
                    target=str(target)[:256],
                    detail=str(detail),
                    ip=ip or "",
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 审计失败不阻塞业务
        log.warning("审计日志写入失败 action=%s user=%s", action, username, exc_info=True)


def client_ip(request) -> str:
    """从 FastAPI Request 取客户端 IP：优先 X-Forwarded-For 首个，回退 X-Real-IP，
    再回退直连对端。反代（nginx 已设 XFF）下才有真实来源；直连时可伪造（内网可接受）。"""
    try:
        if request is None:
            return ""
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
        return request.client.host if request.client else ""
    except Exception:  # noqa: BLE001
        return ""
