"""热度算法（PRD 3.1，2026-09-13 改版）：HotScore = ln(1+Engagement) × TimeDecay

Engagement = views + comments × COMMENT_WEIGHT + likes × LIKE_WEIGHT + shares × SHARE_WEIGHT
TimeDecay  = e^(−λ × 距发布小时数)

不加入人工权重、不加入华军偏好。

改版动因（2026-09-13 审计）：旧版 Engagement 线性进分，一篇爆文会把同窗口其余
文章的归一化分数压扁到个位数（max 归一化对离群值敏感）。log1p 压缩量纲后
"互动量差一个数量级 ≈ 分差 2.3 倍"而非 10 倍，榜单中后段保有区分度；
Reddit hot 公式的 log10 项同理。

零互动降级：来源未公开互动数据时（阅读/分享无公开数据源恒为 0，评论/点赞由
crawler/stats.py 轮询且首采前为 0），取 decay × ln(1+ZERO_REF)。锚定 ZERO_REF=100
表示"零数据 ≈ 100 点原始互动"的新近度——旧版直接 decay×100（原始量纲）会让
刚发布、零数据的新文在安静时段登顶热榜；对数尺度下其分数约 4.6，与一篇
百级互动的文章相当，不再空降。锚定是固定下限、与正枝在 0 处不连续（只有
个位数互动的文章分数低于零数据文章），这是有意的取舍：未首采/无数据
不应比"有真实互动"更亏。

hot_score_expr 提供与 hot_score() 数值一致的 SQL 表达式，供排序/聚合下推
数据库（避免每请求把近期全量文章拉进 Python 打分）。注意 PostgreSQL 的
log() 是 10 底，自然对数必须用 ln()。
"""
import math
from datetime import datetime, timezone

from sqlalchemy import case, func

from app.core.config import get_settings

# 零互动降级的锚定参考量（原始 Engagement 量纲）：零数据文章 ≈ 有 100 点互动
ZERO_REF = 100.0
# math.log1p(ZERO_REF) ≈ 4.615；SQL 侧引用同一常数保证两版本逐位一致
ZERO_REF_LOG = math.log1p(ZERO_REF)


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
        return decay * ZERO_REF_LOG
    return math.log1p(engagement) * decay


def event_hot_from_pairs(pairs) -> float:
    """独立来源口径的事件热度（纯函数参考实现，与 event_service 的 SQL 版逐位对齐）：

    每个 (event, source) 组内取成员最高一篇，再对来源求和——一家媒体发十篇
    只算一次，重复抓取不重复计分，榜单反映「有多少家在说」（AIHOT 语义）。
    pairs: iterable of (source, score)。
    """
    best: dict[str, float] = {}
    for source, score in pairs:
        s = str(source or "")
        v = float(score or 0.0)
        if s not in best or v > best[s]:
            best[s] = v
    return round(sum(best.values()), 2)


def event_rising_state(
    prev_score: float | None,
    prev_at: datetime | None,
    old_score: float,
    new_score: float,
    now: datetime,
    threshold: float = 0.3,
    rolling_hours: float = 6.0,
) -> tuple[float, bool]:
    """事件「上升」滚动脉冲（纯函数）：返回 (应写入的 hot_score_prev, is_rising)。

    滚动基准规则：prev_at 缺失或距 now 超过 rolling_hours（默认 6h，等效
    AIHOT「和 6 小时前比」）时，基准翻新为 old_score（上一个窗口末的分数），
    之后各轮保持不变——基准恒滞后当前 6h~6h+一个重算周期。
    判定：new_score - 基准 > threshold × max(基准, 1.0)——分母下限 1.0 使
    零基准事件需有正向绝对增量才算上升，避免 0→ε 的噪声脉冲。
    naive prev_at 按 UTC 解释（库内时间列均 tz-aware，此分支只为防御）。
    """
    if prev_at is not None and prev_at.tzinfo is None:
        prev_at = prev_at.replace(tzinfo=timezone.utc)
    stale = prev_at is None or (now - prev_at).total_seconds() >= rolling_hours * 3600
    prev = float(old_score or 0.0) if stale else float(prev_score or 0.0)
    rising = (float(new_score or 0.0) - prev) > threshold * max(prev, 1.0)
    return round(prev, 2), bool(rising)


def hot_score_expr(model):
    """hot_score() 的 SQL 版本（SQLAlchemy 列表达式），语义逐项对齐：

    - Engagement 各字段 COALESCE 0；Engagement=0 时取 decay×ln(1+ZERO_REF)（降级策略）；
    - 自然对数用 func.ln（PG 的 log() 是 10 底）；
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
    return case(
        (engagement <= 0, decay * ZERO_REF_LOG),
        else_=func.ln(1 + engagement) * decay,
    )
