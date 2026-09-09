"""科技日报模型（PRD 第 7 节）。"""
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, Integer, UniqueConstraint
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
    content = Column(JSONB, nullable=False)  # {top_events, quick_news, industry_changes, source_stats, top_articles}
    created_at = Column(DateTime(timezone=True), default=_utcnow)
