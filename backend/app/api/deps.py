"""API 公共依赖：当前用户解析与角色校验。"""
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..core.security import decode_token
from ..models import User

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    try:
        payload = decode_token(cred.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录")
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    # 每次请求按主键查库（既有行为），禁用即刻生效；
    # 用户请求量小，一次主键查询可接受，换取无需 token 黑名单/踢出机制
    if user.disabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号已被禁用，请重新登录")
    return user


def require_roles(*roles: str):
    """角色守卫：admin 全通，其余按声明角色放行。"""

    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role == "admin" or user.role in roles:
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")

    return checker


# 常用守卫：写操作至少 operator；用户管理仅 admin
require_operator = require_roles("operator")
require_admin = require_roles()
