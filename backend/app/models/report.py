"""今日早报模型（原「软件日报」，PRD 第 7 节）。"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, Date, DateTime, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class DailyReport(Base):
    __tablename__ = "daily_report"
    __table_args__ = (UniqueConstraint("date", name="uq_report_date"),)

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    issue_no = Column(Integer, default=1)
    # PG 仍为 JSONB；JSON variant 仅为让 exports 早报 feed 等测试能在 SQLite 内存库建表
    # （同 audit_log.detail / event.impacts 先例）
    content = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)  # {top_events, quick_news, industry_changes, source_stats, top_articles}
    created_at = Column(DateTime(timezone=True), default=_utcnow)
