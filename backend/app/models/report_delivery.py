"""日报推送投递记录：按日期、通道和内容哈希实现幂等与可恢复重试。"""
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class ReportDelivery(Base):
    __tablename__ = "report_delivery"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_report_delivery_idempotency"),
        Index("ix_report_delivery_status_next", "status", "next_attempt_at"),
        Index("ix_report_delivery_date_channel", "report_date", "channel"),
    )

    id = Column(Integer, primary_key=True)
    report_date = Column(Date, nullable=False)
    channel = Column(String(16), nullable=False)
    idempotency_key = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    # 快照保证日报后续重算后，失败重试仍发送当初入队的内容；不包含密钥或 Webhook URL。
    content_snapshot = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
