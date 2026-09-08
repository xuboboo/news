"""会话签发与校验。

令牌用 secrets 模块随机生成，库内只存 SHA-256 哈希（库泄露不泄漏可用会话）；
过期/吊销判定全在服务端，改密、禁用、登出均即时生效。
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.user_session import UserSession

SESSION_COOKIE = "hjradar_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """SQLite 等驱动会把 timestamptz 读成 naive UTC，统一补时区再参与 Python 比较。

    psycopg(postgres) 返回 aware，此函数原样放行；仅在本模块/调用方做 Python 侧
    时间比较处使用，SQL 侧过滤不受影响。
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    db: Session, user_id: int, ttl_hours: int, ip: str = "", user_agent: str = ""
) -> tuple[UserSession, str]:
    """签发新会话，返回 (会话行, 明文令牌)。令牌只此一次可见，立即写入 Cookie。"""
    token = secrets.token_urlsafe(32)
    row = UserSession(
        token_hash=_hash_token(token),
        user_id=user_id,
        expires_at=_now() + timedelta(hours=max(1, int(ttl_hours))),
        ip=(ip or "")[:64],
        user_agent=(user_agent or "")[:255],
    )
    db.add(row)
    db.commit()
    return row, token


def get_active_session(db: Session, token: str) -> UserSession | None:
    if not token:
        return None
    row = db.query(UserSession).filter_by(token_hash=_hash_token(token)).first()
    if row is None or row.revoked_at is not None:
        return None
    expires = as_aware(row.expires_at)
    if expires is not None and expires <= _now():
        return None
    return row


def touch(row: UserSession) -> None:
    """活跃度打点：距上次 >60s 才写库，避免轮询请求逐条产生 UPDATE。"""
    now = _now()
    last_seen = as_aware(row.last_seen_at)
    if last_seen is None or (now - last_seen).total_seconds() > 60:
        row.last_seen_at = now
        row._touched = True  # 标记由调用方统一 commit


def revoke_session(db: Session, row: UserSession) -> None:
    if row.revoked_at is None:
        row.revoked_at = _now()
        db.commit()


def revoke_user_sessions(db: Session, user_id: int, except_session_id: int | None = None) -> int:
    """吊销某用户全部会话（改密/禁用时调用）；except_session_id 保留当前会话。"""
    q = db.query(UserSession).filter(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if except_session_id is not None:
        q = q.filter(UserSession.id != except_session_id)
    count = q.update({"revoked_at": _now()}, synchronize_session=False)
    db.commit()
    return count


def revoke_tenant_sessions(db: Session, tenant_id: int) -> int:
    """吊销某租户全部用户的会话（停用租户时调用）。

    停用期间 _authenticate 的租户状态检查已能拒绝请求，但会话行仍存活——
    若不吊销，重新启用租户的瞬间这些会话全部"复活"（最长可达绝对 TTL），
    含停用前已泄露的令牌。
    """
    from app.models.user import User

    count = (
        db.query(UserSession)
        .filter(
            UserSession.revoked_at.is_(None),
            UserSession.user_id.in_(db.query(User.id).filter(User.tenant_id == tenant_id)),
        )
        .update({"revoked_at": _now()}, synchronize_session=False)
    )
    db.commit()
    return count


def purge_expired(db: Session) -> int:
    """物理删除已过期或已吊销的会话行（beat 每周调用）。"""
    count = (
        db.query(UserSession)
        .filter(or_(UserSession.expires_at <= _now(), UserSession.revoked_at.isnot(None)))
        .delete(synchronize_session=False)
    )
    db.commit()
    return count
