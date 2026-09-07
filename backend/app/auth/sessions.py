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
    if row.expires_at is not None and row.expires_at <= _now():
        return None
    return row


def touch(row: UserSession) -> None:
    """活跃度打点：距上次 >60s 才写库，避免轮询请求逐条产生 UPDATE。"""
    now = _now()
    if row.last_seen_at is None or (now - row.last_seen_at).total_seconds() > 60:
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


def purge_expired(db: Session) -> int:
    """物理删除已过期或已吊销的会话行（beat 每周调用）。"""
    count = (
        db.query(UserSession)
        .filter(or_(UserSession.expires_at <= _now(), UserSession.revoked_at.isnot(None)))
        .delete(synchronize_session=False)
    )
    db.commit()
    return count
