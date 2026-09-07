"""登录会话：不透明令牌只存 SHA-256 哈希，Cookie 携带原令牌（即时可吊销）。"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class UserSession(Base):
    __tablename__ = "user_session"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # sha256 hex
    user_id = Column(Integer, ForeignKey("app_user.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at = Column(DateTime(timezone=True), default=_utcnow)
    ip = Column(String(64), nullable=False, default="")
    user_agent = Column(String(255), nullable=False, default="")
    revoked_at = Column(DateTime(timezone=True))
