"""认证：登录签发 JWT、当前用户信息、改密。"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..core import audit, ratelimit
from ..core.database import get_db
from ..core.security import create_token, hash_password, verify_password
from ..models import User
from .deps import get_current_user
from .schemas import LoginIn, PasswordChangeIn, TokenOut, UserOut

router = APIRouter(prefix="/api/auth", tags=["认证"])

# 登录时序补偿：用户不存在时也跑一次 PBKDF2，消除「用户不存在 vs 密码错误」的响应时间差
_DUMMY_HASH = hash_password("timing-equalization-dummy")


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = audit.client_ip(request)
    wait = ratelimit.check_login_allowed(ip, body.username)
    if wait is not None:
        audit.record(body.username, "login_rate_limited",
                     detail=f"等待 {int(wait) + 1} 秒", ip=ip)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"尝试次数过多，请约 {int(wait) + 1} 秒后重试")
    user = db.query(User).filter(User.username == body.username).first()
    if user is None:
        # 用户不存在也执行一次 PBKDF2，对齐错误密码分支的耗时，避免用户名枚举时序差
        verify_password(body.password, _DUMMY_HASH)
        ratelimit.note_failure(ip, body.username)
        audit.record(body.username, "login_failed", ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not verify_password(body.password, user.password_hash):
        ratelimit.note_failure(ip, body.username)
        audit.record(body.username, "login_failed", ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if user.disabled:
        ratelimit.note_failure(ip, body.username)
        audit.record(user.username, "login_failed", detail="账号已禁用", ip=ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用，请联系管理员")
    ratelimit.note_success(ip, user.username)
    audit.record(user.username, "login", ip=ip)
    return TokenOut(
        token=create_token(user.id, user.username, user.role),
        username=user.username,
        role=user.role,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password")
def change_password(
    body: PasswordChangeIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "原密码错误")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    audit.record(user.username, "password_change", ip=audit.client_ip(request))
    return {"ok": True}
