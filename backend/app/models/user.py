"""平台用户：归属租户，三级 RBAC。账号只由管理员后台创建，无注册入口。

表名 app_user 避开 PostgreSQL 保留字 user。
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.core.db import Base

ROLE_SUPER_ADMIN = "super_admin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_MEMBER = "member"
ROLES = (ROLE_SUPER_ADMIN, ROLE_TENANT_ADMIN, ROLE_MEMBER)


def _utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "app_user"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True)  # 存储前统一小写归一
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(64), nullable=False, default="")
    tenant_id = Column(Integer, ForeignKey("tenant.id"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default=ROLE_MEMBER)
    status = Column(String(16), nullable=False, default="active")  # active / disabled
    must_change_password = Column(Boolean, nullable=False, default=True)  # 建号/重置后首登强制改密
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True))
    last_login_at = Column(DateTime(timezone=True))
    created_by = Column(String(255), nullable=False, default="")  # 创建者邮箱 / bootstrap
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
