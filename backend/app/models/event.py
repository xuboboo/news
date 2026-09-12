"""热点事件模型（PRD 第 9 节：event / event_article 表）。"""
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer,
                        JSON, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB

from app.core.config import get_settings
from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class HotEvent(Base):
    __tablename__ = "event"

    id = Column(Integer, primary_key=True)
    title = Column(String(512), nullable=False)
    summary = Column(Text, default="")            # AI 事件总结
    # PG 仍为 JSONB；JSON variant 仅为让事件自愈等测试能在 SQLite 内存库上建表
    # （同 audit_log.detail / article_summary 先例）
    impacts = Column(JSON().with_variant(JSONB, "postgresql"), default=list)   # 核心影响
    hot_score = Column(Float, default=0)
    first_time = Column(DateTime(timezone=True))  # 最早报道时间
    last_time = Column(DateTime(timezone=True), index=True)
    status = Column(String(16), default="发酵中")  # 发酵中 / 持续关注 / 已平息
    timeline = Column(JSON().with_variant(JSONB, "postgresql"), default=list)  # [{time, text, source}]
    centroid = Column(Vector(get_settings().embed_dim))  # 事件向量质心（增量聚合用）
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class EventArticle(Base):
    __tablename__ = "event_article"
    __table_args__ = (UniqueConstraint("event_id", "article_id", name="uq_event_article"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("event.id", ondelete="CASCADE"), nullable=False, index=True)
    article_id = Column(Integer, ForeignKey("article.id", ondelete="CASCADE"), nullable=False, index=True)
