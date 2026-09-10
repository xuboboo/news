"""热度算法（PRD 3.1）：HotScore = Engagement × TimeDecay

Engagement = views + comments × COMMENT_WEIGHT + likes × LIKE_WEIGHT + shares × SHARE_WEIGHT
TimeDecay  = e^(−λ × 距发布小时数)

不加入人工权重、不加入华军偏好。

hot_score_expr 提供与 hot_score() 数值一致的 SQL 表达式，供排序/聚合下推
数据库（避免每请求把近期全量文章拉进 Python 打分）。
"""
import math
from datetime import datetime, timezone

from sqlalchemy import case, func

from app.core.config import get_settings


def raw_engagement(views: int, comments: int, shares: int, likes: int = 0) -> float:
    s = get_settings()
    return (
        (views or 0)
        + (comments or 0) * s.comment_weight
        + (likes or 0) * s.like_weight
        + (shares or 0) * s.share_weight
    )


def time_decay(publish_time: datetime, now: datetime | None = None) -> float:
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    if publish_time.tzinfo is None:
        publish_time = publish_time.replace(tzinfo=timezone.utc)
    hours = max(0.0, (now - publish_time).total_seconds() / 3600)
    return math.exp(-s.decay_per_hour * hours)


def hot_score(
    views: int, comments: int, shares: int, likes: int, publish_time: datetime,
    now: datetime | None = None,
) -> float:
    engagement = raw_engagement(views, comments, shares, likes)
    decay = time_decay(publish_time, now)
    if engagement <= 0:
        # 来源未公开互动数据时的降级策略：以时间衰减为主导的真实热度
        # （评论/点赞由 crawler/stats.py 轮询，阅读/分享无公开数据源恒为 0）
        return decay * 100.0
    return engagement * decay


def hot_score_expr(model):
    """hot_score() 的 SQL 版本（SQLAlchemy 列表达式），语义逐项对齐：

    - Engagement 各字段 COALESCE 0；Engagement=0 时取 decay×100（降级策略）；
    - 时距取 epoch 秒差，负值（未来发布时间）按 0 处理，与 Python max(0.0, ...) 一致。
    排序/取极值请用它（DB 内完成），逐篇展示仍可用 Python 版。
    """
    s = get_settings()
    engagement = (
        func.coalesce(model.views, 0)
        + func.coalesce(model.comments, 0) * s.comment_weight
        + func.coalesce(model.likes, 0) * s.like_weight
        + func.coalesce(model.shares, 0) * s.share_weight
    )
    hours = func.extract("epoch", func.now() - model.publish_time) / 3600.0
    decay = func.exp(-s.decay_per_hour * func.greatest(hours, 0.0))
    return case((engagement <= 0, decay * 100.0), else_=engagement * decay)
