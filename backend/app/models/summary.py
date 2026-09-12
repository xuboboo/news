"""AI 摘要模型（PRD 第 6 节：article_summary 表）。"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class ArticleSummary(Base):
    __tablename__ = "article_summary"
    __table_args__ = (UniqueConstraint("article_id", name="uq_summary_article"),)

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, ForeignKey("article.id", ondelete="CASCADE"), nullable=False, index=True)
    summary_short = Column(Text, default="")   # 一句话摘要
    summary_long = Column(Text, default="")    # 长摘要（备用/详情）
    # PG 仍为 JSONB；JSON variant 仅为让摘要自愈等测试能在 SQLite 内存库上建表
    # （同 audit_log.detail 先例）
    key_points = Column(JSON().with_variant(JSONB, "postgresql"), default=list)   # 核心信息 3-5 条
    tags = Column(JSON().with_variant(JSONB, "postgresql"), default=list)         # 标签
    model = Column(String(128), default="")    # 生成模型标识
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    article = relationship("Article", back_populates="summary")
