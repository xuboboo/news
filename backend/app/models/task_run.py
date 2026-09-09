"""任务执行记录（管理面板"任务执行记录"页签的数据源）。

只记录粗粒度任务（crawler / event / report），逐篇 summary/embedding 不记录。
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class TaskRun(Base):
    __tablename__ = "task_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, index=True)   # crawler / event / report
    trigger = Column(String(16), nullable=False, default="auto")  # auto / manual
    status = Column(String(16), nullable=False, default="running")  # running / success / degraded / failed / stale
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    detail = Column(Text, nullable=True)   # 成功：简短结果摘要；失败：错误信息
