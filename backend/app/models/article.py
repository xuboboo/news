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
    likes = Column(BigInteger, default=0)                      # 「有价值」点赞数（IT之家文章价值打分）
    # 互动数据轮询状态（crawler/stats.py）：
    # 待采集/回填 = polled_at IS NULL 且 next_poll_at IS NULL；到期 = next_poll_at <= now；
    # 停采终态（超 14 天窗或连续失败放弃）= next_poll_at IS NULL 且 polled_at 非空
    stats_polled_at = Column(DateTime(timezone=True))
    stats_next_poll_at = Column(DateTime(timezone=True), index=True)
    stats_fail_count = Column(Integer, default=0)
    embedding = Column(Vector(get_settings().embed_dim))
    # AI 处理状态：pending → summarized → embedded → clustered
    ai_status = Column(String(16), default="pending", index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    summary = relationship(
        "ArticleSummary", uselist=False, cascade="all, delete-orphan", back_populates="article"
    )
