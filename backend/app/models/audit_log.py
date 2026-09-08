"""审计日志：登录全路径 + 管理写操作，谁在何时从哪里做了什么。

不带外键（与 ai_usage_log 同风格）：账号/租户删除后日志仍可读，邮箱冗余存储。

防篡改：prev_hash → row_hash 逐行哈希链（row_hash = sha256(prev_hash‖行内容)），
行被修改/中间行被删除在校验时必然断链；scripts/audit_chain_verify.py 离线校验。
row_hash 为空的行是哈希链上线前的遗留段。
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, index=True)
    user_id = Column(Integer, index=True)  # 操作者；匿名登录失败等场景可空
    user_email = Column(String(255), nullable=False, default="")
    tenant_id = Column(Integer, index=True)
    action = Column(String(64), nullable=False, index=True)
    # PG 仍为 JSONB；JSON variant 仅为让测试能在 SQLite 内存库上建表
    detail = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict)
    ip = Column(String(64), nullable=False, default="")
    user_agent = Column(String(255), nullable=False, default="")
    prev_hash = Column(String(64), nullable=False, default="")
    row_hash = Column(String(64), nullable=False, default="")
