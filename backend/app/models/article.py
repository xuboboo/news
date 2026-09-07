"""文章模型（对应数据库：文章库 + Embedding 向量列）。"""
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.config import get_settings
from app.core.db import Base
from app.models import summary as _summary_model  # noqa: F401  确保 relationship 字符串可解析


def _utcnow():
    return datetime.now(timezone.utc)


class Article(Base):
    __tablename__ = "article"

    id = Column(Integer, primary_key=True)
    title = Column(String(512), nullable=False)
    url = Column(String(1024), nullable=False, unique=True, index=True)
    channel = Column(String(64), nullable=False, index=True)   # 软件之家 / Win11之家 / Win10之家 / 智能时代
    source = Column(String(64), nullable=False, default="IT之家")
    news_id = Column(String(64), index=True)                   # 原始站点文章 ID
    content = Column(Text, default="")
    publish_time = Column(DateTime(timezone=True), nullable=False, index=True)
    views = Column(BigInteger, default=0)
    comments = Column(BigInteger, default=0)
    shares = Column(BigInteger, default=0)
    embedding = Column(Vector(get_settings().embed_dim))
    # AI 处理状态：pending → summarized → embedded → clustered
    ai_status = Column(String(16), default="pending", index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    summary = relationship(
        "ArticleSummary", uselist=False, cascade="all, delete-orphan", back_populates="article"
    )
