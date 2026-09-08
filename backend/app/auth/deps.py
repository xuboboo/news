"""鉴权依赖链：require_user → require_tenant_admin → require_super_admin。

会话载体：HttpOnly Cookie（SameSite=Lax）。当前用户/会话挂到 request.state，
供审计埋点与业务端点复用。
"""
import hmac
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import sessions as session_store
from app.core.config import get_settings
from app.core.db import get_db
from app.services import config_service
from app.models.tenant import Tenant
from app.models.user import (
    ROLE_MEMBER,
    ROLE_SUPER_ADMIN,
    ROLE_TENANT_ADMIN,
    User,
)

# token 应急通道只等效 /api/v2/admin/*（运维面板与脚本回归的用途边界）；
# 业务接口不接受 token——泄露一枚运维令牌不应等于全站超管
_TOKEN_BACKDOOR_PREFIX = "/api/v2/admin"


def _effective_int(db: Session, key: str, fallback: int) -> int:
    try:
        return int(config_service.get_effective(db, key))
    except Exception:
        return fallback


def authenticate_session(request: Request, db: Session) -> tuple[User, object]:
    """Cookie → 会话行 → 用户；校验会话有效性、空闲超时、账号与租户状态。

    全站唯一的会话校验实现（admin.require_admin 复用，避免双实现漂移）。
    校验通过后做活跃度打点（>60s 才写库）。
    """
    token = request.cookies.get(session_store.SESSION_COOKIE, "")
    row = session_store.get_active_session(db, token)
    if row is None:
        raise HTTPException(401, "未登录或会话已失效")
    # 空闲超时：连续无请求超过 session_idle_hours 即失效（0 = 关闭）。
    # OWASP 会话管理：绝对 TTL 之外还须有 inactivity timeout，限制被劫持会话的可用窗口
    idle_hours = _effective_int(db, "session_idle_hours", get_settings().session_idle_hours)
    if idle_hours > 0:
        last_seen = session_store.as_aware(row.last_seen_at)
        if last_seen is not None and last_seen < session_store._now() - timedelta(hours=idle_hours):
            session_store.revoke_session(db, row)
            raise HTTPException(401, "会话长时间未活动已失效，请重新登录")
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


def _admin_token_ok(request: Request, db: Session) -> bool:
    """X-Admin-Token 应急通道校验（与 admin.require_admin 同源，供脚本/回归使用）。"""
    token = request.headers.get("x-admin-token") or ""
    if not token:
        return False
    try:
        effective = config_service.get_effective(db, "admin_token")
        if not effective:
            return False
        return hmac.compare_digest(token.encode(), str(effective).encode())
    except Exception:
        return False


class _TokenPrincipal:
    """X-Admin-Token 应急后门的虚拟主体：等价平台超管。

    返回真实 User 对象形状（而非 None），使所有使用 actor 的端点
    （users.py 的 actor.role/email/tenant_id、审计埋点 user_profile 等）
    在后门通道下正常工作而不需要特判。
    """

    id = None
    email = "admin-token"
    display_name = "admin-token"
    role = ROLE_SUPER_ADMIN
    tenant_id = None
    status = "active"
    must_change_password = False
    last_login_at = None


def attach_token_principal(request: Request) -> "_TokenPrincipal":
    """X-Admin-Token / 免鉴权通道挂虚拟主体（等价平台超管）。

    使审计埋点能记到「admin-token」而非空主体——config_updated 等最敏感操作
    经应急通道执行时必须可追溯（2026-09-09 审计修复）。已有真实登录主体时不覆盖
    （routes.py 全局 require_user 先行挂载、端点再过 require_admin 的场景）。
    """
    existing = getattr(request.state, "current_user", None)
    if existing is not None:
        return existing
    principal = _TokenPrincipal()
    request.state.current_user = principal
    return principal


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    try:
        user, row = authenticate_session(request, db)
    except HTTPException as exc:
        if (
            exc.status_code == 401
            and request.url.path.startswith(_TOKEN_BACKDOOR_PREFIX)
            and _admin_token_ok(request, db)
        ):
            # 应急后门：token 视为超管等价（虚拟主体），脚本无需 Cookie 会话
            return attach_token_principal(request)
        raise
    # 首登强制改密：服务端兜底（前端仅是引导），只放行认证域接口
    if getattr(user, "must_change_password", False) and not request.url.path.startswith(
        "/api/v2/auth"
    ):
        raise HTTPException(403, "首次登录请先修改密码")
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
    "authenticate_session",
    "current_user_of",
    "attach_token_principal",
    "ROLE_MEMBER",
    "ROLE_TENANT_ADMIN",
    "ROLE_SUPER_ADMIN",
]
