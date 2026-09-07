"""鉴权依赖链：require_user → require_tenant_admin → require_super_admin。

会话载体：HttpOnly Cookie（SameSite=Lax）。当前用户/会话挂到 request.state，
供审计埋点与业务端点复用。
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import sessions as session_store
from app.core.db import get_db
from app.models.tenant import Tenant
from app.models.user import (
    ROLE_MEMBER,
    ROLE_SUPER_ADMIN,
    ROLE_TENANT_ADMIN,
    User,
)


def _authenticate(request: Request, db: Session) -> tuple[User, object]:
    """Cookie → 会话行 → 用户；校验会话有效性、账号与租户状态。"""
    token = request.cookies.get(session_store.SESSION_COOKIE, "")
    row = session_store.get_active_session(db, token)
    if row is None:
        raise HTTPException(401, "未登录或会话已失效")
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise HTTPException(401, "账号不可用或已停用")
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise HTTPException(403, "所属租户已停用，请联系平台管理员")
    session_store.touch(row)
    if getattr(row, "_touched", False):
        db.commit()
    return user, row


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user, row = _authenticate(request, db)
    request.state.current_user = user
    request.state.current_session = row
    return user


def require_tenant_admin(
    request: Request, user: User = Depends(require_user)
) -> User:
    if user.role not in (ROLE_TENANT_ADMIN, ROLE_SUPER_ADMIN):
        raise HTTPException(403, "需要租户管理员权限")
    return user


def require_super_admin(
    request: Request, user: User = Depends(require_user)
) -> User:
    if user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(403, "需要平台超管权限")
    return user


def current_user_of(request: Request) -> User | None:
    """已鉴权请求里取当前用户（未鉴权返回 None），供审计埋点用。"""
    return getattr(request.state, "current_user", None)


def require_json_mutation(request: Request) -> None:
    """变更接口 CSRF 防线：自定义头 X-Requested-With + JSON content-type。

    SameSite=Lax 已使跨站表单不携带 Cookie，这里再加一道显式校验；
    GET/HEAD/OPTIONS 与无 body 的 DELETE 不受影响。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if (request.headers.get("x-requested-with") or "").lower() != "xmlhttprequest":
        raise HTTPException(403, "缺少 X-Requested-With 头（CSRF 防护）")
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type and not content_type.startswith("application/json"):
        raise HTTPException(415, "仅接受 application/json 请求体")


__all__ = [
    "require_user",
    "require_tenant_admin",
    "require_super_admin",
    "require_json_mutation",
    "current_user_of",
    "ROLE_MEMBER",
    "ROLE_TENANT_ADMIN",
    "ROLE_SUPER_ADMIN",
]
