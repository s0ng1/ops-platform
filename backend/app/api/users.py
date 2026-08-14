"""用户管理（仅 admin）。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..core.security import hash_password
from ..models import User
from .deps import require_admin
from .schemas import UserCreateIn, UserOut, UserUpdateIn

router = APIRouter(
    prefix="/api/users", tags=["用户"], dependencies=[Depends(require_admin)]
)


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.record(admin.username, "user_create", target=user.username,
                 detail=f"role={user.role}", ip=audit.client_ip(request))
    return user


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """改角色 / 禁用启用（仅 admin）。
    防护：不能禁用或降级自己；系统须至少保留一个启用状态的 admin。
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    new_role = body.role if body.role is not None else user.role
    new_disabled = body.disabled if body.disabled is not None else user.disabled

    demoting = new_role != "admin" and user.role == "admin"
    disabling = new_disabled and not user.disabled
    if user.id == admin.id and (demoting or disabling):
        raise HTTPException(status_code=400, detail="不能禁用或降级自己")
    if (demoting or disabling) and user.role == "admin" and not user.disabled:
        active_admins = (
            db.query(User)
            .filter(User.role == "admin", User.disabled.is_(False), User.id != user.id)
            .count()
        )
        if active_admins == 0:
            raise HTTPException(status_code=400, detail="至少保留一个启用状态的管理员")

    changes = []
    if new_role != user.role:
        audit.record(admin.username, "user_update", target=user.username,
                     detail=f"role: {user.role} -> {new_role}", ip=audit.client_ip(request))
        changes.append(f"角色 {user.role}→{new_role}")
        user.role = new_role
    if new_disabled != user.disabled:
        action = "user_disable" if new_disabled else "user_enable"
        audit.record(admin.username, action, target=user.username, ip=audit.client_ip(request))
        changes.append("禁用" if new_disabled else "启用")
        user.disabled = new_disabled
    if changes:
        db.commit()
        db.refresh(user)
    return user


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if db.query(User).count() <= 1:
        raise HTTPException(status_code=400, detail="至少保留一个用户")
    name = user.username
    db.delete(user)
    db.commit()
    audit.record(admin.username, "user_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}
