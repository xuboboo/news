"""运行时系统配置（管理面板可改，优先于 .env 默认值）。"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class SystemSetting(Base):
    __tablename__ = "system_setting"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
