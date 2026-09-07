"""登录/登出/改密业务：失败锁定、会话轮换、审计埋点。"""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.auth import passwords
from app.auth import sessions as session_store
from app.core.config import get_settings
from app.models.user import User
from app.services import audit_service, config_service

MIN_PASSWORD_LEN = 8
# bcrypt 只取输入前 72 字节：超长部分会被静默截断，改为显式拒绝
MAX_PASSWORD_LEN = 72

# 防用户枚举的时序拉平桩：对不存在的邮箱也执行一次同成本 bcrypt 校验，
# 使「邮箱不存在」与「密码错误」的响应耗时一致（模块导入时算一次，约 200ms）
_ENUM_GUARD_HASH = passwords.hash_password("timing-equalizer-dummy-account")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _effective_int(db: Session, key: str, fallback: int) -> int:
    try:
        return int(config_service.get_effective(db, key))
    except Exception:
        return fallback


def session_ttl_hours(db: Session) -> int:
    return _effective_int(db, "session_ttl_hours", get_settings().session_ttl_hours)


def _set_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    response.set_cookie(
        key=session_store.SESSION_COOKIE,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        path="/",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(session_store.SESSION_COOKIE, path="/")


def login(db: Session, request: Request, response: Response, email: str, password: str) -> User:
    """邮箱+密码登录。失败路径全部落审计；成功轮换新会话（防会话固定）。"""
    email_norm = (email or "").strip().lower()
    user = db.query(User).filter_by(email=email_norm).first()

    def _fail(action: str, status: int, message: str, extra: dict | None = None) -> None:
        audit_service.record(
            db, action, request=request, user_id=user.id if user else None,
            user_email=email_norm, tenant_id=user.tenant_id if user else None,
            detail=extra or {},
        )
        raise HTTPException(status, message)

    if user is None or user.status != "active":
        passwords.verify_password(password or "", _ENUM_GUARD_HASH)  # 时序拉平，防枚举
        _fail("auth.login_failed", 401, "邮箱或密码错误")

    # 租户停用必须在登录口明确拒绝：否则用户"登录成功"后每个请求都 403，
    # 前端只会反复弹回登录页且无任何提示（2026-09-08 审计缺陷 A）
    from app.models.tenant import Tenant
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        _fail("auth.login_failed", 403, "所属租户已停用，请联系平台管理员",
              {"tenant_disabled": True})

    if user.locked_until and user.locked_until > _now():
        retry_min = max(1, int((user.locked_until - _now()).total_seconds() // 60) + 1)
        _fail("auth.locked", 423, f"账号已临时锁定，请约 {retry_min} 分钟后重试")

    if not passwords.verify_password(password or "", user.password_hash):
        max_attempts = _effective_int(db, "login_max_attempts", get_settings().login_max_attempts)
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= max_attempts:
            lock_minutes = _effective_int(db, "login_lock_minutes", get_settings().login_lock_minutes)
            user.locked_until = _now() + timedelta(minutes=lock_minutes)
            user.failed_attempts = 0  # 锁定到期后重新计数
            db.commit()
            _fail("auth.locked", 423, f"连续失败 {max_attempts} 次，账号锁定 {lock_minutes} 分钟")
        db.commit()
        _fail("auth.login_failed", 401, "邮箱或密码错误")

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = _now()
    db.commit()

    ttl_hours = session_ttl_hours(db)
    row, token = session_store.create_session(
        db, user.id, ttl_hours,
        ip=audit_service.client_ip(request),
        user_agent=audit_service.user_agent(request),
    )
    _set_cookie(response, token, ttl_hours * 3600)
    audit_service.record(
        db, "auth.login", request=request, user=user, detail={"session_id": row.id}
    )
    return user


def logout(db: Session, request: Request, response: Response, user: User) -> None:
    row = getattr(request.state, "current_session", None)
    if row is not None:
        session_store.revoke_session(db, row)
    clear_cookie(response)
    audit_service.record(
        db, "auth.logout", request=request, user=user, detail={"session_id": row.id if row else None}
    )


def change_password(
    db: Session, request: Request, user: User, old_password: str, new_password: str
) -> None:
    if not passwords.verify_password(old_password or "", user.password_hash):
        raise HTTPException(400, "当前密码错误")
    if len(new_password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"新密码至少 {MIN_PASSWORD_LEN} 位")
    if len((new_password or "").encode("utf-8")) > MAX_PASSWORD_LEN:
        raise HTTPException(400, "新密码过长（最多 72 字节）")
    user.password_hash = passwords.hash_password(new_password)
    user.must_change_password = False
    user.failed_attempts = 0
    user.locked_until = None
    db.commit()
    current = getattr(request.state, "current_session", None)
    session_store.revoke_user_sessions(
        db, user.id, except_session_id=current.id if current else None
    )
    audit_service.record(db, "auth.password_changed", request=request, user=user)


def user_profile(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "must_change_password": bool(user.must_change_password),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
