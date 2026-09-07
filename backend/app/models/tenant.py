"""租户（组织）：多租户权限的顶层隔离单元。资讯数据全局共享，租户只隔离用户与权限。"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenant"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="active")  # active / disabled
    remark = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
