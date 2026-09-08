"""AI 调用计量模型（Token 统计与成本计费）。

ai_usage_log：每次 LLM / Embedding 调用（成功、失败、降级）落一条流水；
费用在入库时按 model_price 当时单价算好，调价只影响之后的新记录（历史账单锁定）。
article_id / event_id 不设外键：账单是独立财务记录，文章/事件被删除后流水仍保留。
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, Numeric, String, Text

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class AiUsageLog(Base):
    __tablename__ = "ai_usage_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, index=True)
    task_type = Column(String(32), nullable=False, index=True)    # summary / event_summary / embedding（report 预留）
    trigger = Column(String(16), nullable=False, default="auto")  # auto / manual_repair
    model = Column(String(64), nullable=False, default="")        # 实际模型（降级也记，供 LLM 覆盖率统计）
    article_id = Column(Integer, index=True, nullable=True)
    event_id = Column(Integer, index=True, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    status = Column(String(24), nullable=False, default="success")  # success / api_fail / parse_fail
    error = Column(Text, nullable=True)
    cost = Column(Numeric(12, 6), default=0)  # 人民币元，入库时锁定


class ModelPrice(Base):
    __tablename__ = "model_price"

    model = Column(String(64), primary_key=True)
    price_input = Column(Numeric(12, 4), nullable=False, default=0)   # 每百万 token 输入单价（元）
    price_output = Column(Numeric(12, 4), nullable=False, default=0)  # 每百万 token 输出单价（元；embedding 为 0）
    price_cache = Column(Numeric(12, 4), nullable=True)               # 缓存命中输入单价（元）；NULL=按输入价全价计
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
