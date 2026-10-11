"""HotScore 热度算法边界测试（2026-09-09 审计补齐；2026-09-13 适配 log1p 改版）。

覆盖：原始互动加权、时间衰减、零互动锚定分、log1p 量纲压缩、未来发布时间、
SQL 表达式与 Python 版结构语义一致性。纯函数为主，不依赖数据库。

离线运行：python -m pytest tests/test_hot_score.py -q
"""
import math
from datetime import datetime, timedelta, timezone

from app.algorithms.hot_score import (
    ZERO_REF,
    ZERO_REF_LOG,
    hot_score,
    hot_score_expr,
    raw_engagement,
    time_decay,
)
from app.core.config import get_settings

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
S = get_settings()


# ———— raw_engagement：加权 ————

def test_raw_engagement_default_weights():
    # views + comments*4 + likes*2 + shares*6（与 config 默认权重一致）
    assert raw_engagement(100, 10, 5, 3) == 100 + 40 + 6 + 30


def test_raw_engagement_none_treated_as_zero():
    assert raw_engagement(0, 0, 0, 0) == 0
    assert raw_engagement(None, None, None, None) == 0


# ———— time_decay：衰减 ————

def test_time_decay_now_is_one():
    assert time_decay(NOW, now=NOW) == 1.0


def test_time_decay_monotonic_with_age():
    fresh = time_decay(NOW - timedelta(hours=1), now=NOW)
    old = time_decay(NOW - timedelta(hours=48), now=NOW)
    assert 0 < old < fresh < 1.0
    # λ=decay_per_hour：48 小时衰减 e^(-48λ)（2026-09-10 起 λ=0.02，半衰期 ≈34.7h）
    assert math.isclose(old, math.exp(-S.decay_per_hour * 48), rel_tol=1e-9)


def test_time_decay_naive_time_assumed_utc():
    naive = datetime(2026, 9, 9, 11, 0)  # 无时区
    assert time_decay(naive, now=NOW) == time_decay(NOW - timedelta(hours=1), now=NOW)


def test_time_decay_future_publish_clamped():
    """未来发布时间（时钟漂移/脏数据）：负时距按 0 计，衰减不放大。"""
    assert time_decay(NOW + timedelta(hours=5), now=NOW) == 1.0


# ———— hot_score：零互动锚定分（log1p 改版） ————

def test_hot_score_zero_engagement_anchors_at_log1p_zero_ref():
    """零互动锚定分 = ln(1+ZERO_REF)×时间衰减（2026-09-13 改版）。

    旧版 decay×100（原始量纲）让零数据新文在安静时段以 100 原始分登顶；
    对数尺度下锚定值 ≈4.6，与百级互动文章相当，不再空降。
    """
    fresh = hot_score(0, 0, 0, 0, NOW, now=NOW)
    assert math.isclose(fresh, ZERO_REF_LOG, rel_tol=1e-9)
    assert fresh < 5.0  # 远低于旧版线性量纲的 100
    day_old = hot_score(0, 0, 0, 0, NOW - timedelta(hours=24), now=NOW)
    assert math.isclose(
        day_old, ZERO_REF_LOG * math.exp(-S.decay_per_hour * 24), rel_tol=1e-9
    )
    assert day_old < fresh


def test_hot_score_engagement_log1p_times_decay():
    raw = raw_engagement(100, 10, 5, 3)
    assert math.isclose(
        hot_score(100, 10, 5, 3, NOW - timedelta(hours=2), now=NOW),
        math.log1p(raw) * math.exp(-S.decay_per_hour * 2),
        rel_tol=1e-9,
    )


def test_hot_score_log1p_compresses_outliers():
    """量纲压缩性质：互动量差 10 倍 → 分差 ≈ ln(10)（≈2.3），而非 10 倍。

    这是本次改版的核心动因：max 归一化下线性量纲的爆文会把全榜压扁。
    """
    fresh_big = hot_score(10_000, 0, 0, 0, NOW, now=NOW)
    modest = hot_score(1_000, 0, 0, 0, NOW, now=NOW)
    assert math.isclose(fresh_big - modest, math.log(10), rel_tol=1e-3)
    # 榜单中后段保有区分度：第 10 名不会被第一名压到 1/10
    assert modest > 0.5 * fresh_big


def test_hot_score_zero_engagement_equals_zero_ref_article():
    """锚定语义：零数据文章与恰好 ZERO_REF 点原始互动（100 阅读）的文章同分。"""
    anchored = hot_score(0, 0, 0, 0, NOW, now=NOW)
    with_ref = hot_score(ZERO_REF, 0, 0, 0, NOW, now=NOW)
    assert math.isclose(anchored, with_ref, rel_tol=1e-9)


# ———— SQL 表达式与 Python 版一致性 ————

def test_hot_score_expr_compiles_with_same_semantics():
    """表达式可编译且结构语义对齐（COALESCE 0 / greatest 钳负 / ln / 保底分支）。"""
    from app.models.article import Article

    expr = hot_score_expr(Article)
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "coalesce" in sql.lower()
    assert "greatest" in sql.lower()  # 未来发布时间钳 0，与 Python max(0.0, ...) 一致
    assert "exp" in sql.lower()
    # PostgreSQL 的 log() 是 10 底，自然对数必须编译为 ln
    assert " ln(" in f" {sql.lower()} " or "ln(" in sql.lower()


# ———— 事件热度改版（2026-10-12，AIHOT「按事件算热度」语义）————

from app.algorithms.hot_score import event_hot_from_pairs, event_rising_state  # noqa: E402


class TestEventHotFromPairs:
    """独立来源口径：每来源计成员最高一篇再求和。"""

    def test_same_source_counts_once(self):
        # 一家媒体发十篇只算一次（取最高）
        pairs = [("IT之家", 5.0), ("IT之家", 9.0), ("IT之家", 2.0)]
        assert event_hot_from_pairs(pairs) == 9.0

    def test_cross_sources_sum(self):
        pairs = [("IT之家", 5.0), ("快科技", 3.0)]
        assert event_hot_from_pairs(pairs) == 8.0

    def test_none_source_and_scores_tolerated(self):
        pairs = [(None, None), ("", 0.0), ("IT之家", 1.5)]
        assert event_hot_from_pairs(pairs) == 1.5

    def test_rounding_two_decimals(self):
        assert event_hot_from_pairs([("a", 1.111), ("b", 2.222)]) == 3.33

    def test_empty(self):
        assert event_hot_from_pairs([]) == 0


class TestEventRisingState:
    """滚动基准：prev 恒滞后 6h~6h+一个重算周期；上升=新分较基准涨幅超阈值。"""

    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def test_first_round_initializes_baseline_from_old_score(self):
        # 无 prev：基准取 old_score（上一窗口末），本轮上涨即判上升
        prev, rising = event_rising_state(None, None, old_score=1.0, new_score=5.0, now=self.NOW)
        assert prev == 1.0
        assert rising is True  # (5-1) > 0.3*1

    def test_within_window_keeps_prev(self):
        prev_at = self.NOW - timedelta(minutes=10)
        prev, rising = event_rising_state(4.0, prev_at, old_score=4.5, new_score=5.0, now=self.NOW)
        assert prev == 4.0  # 基准不翻新
        assert rising is False  # (5-4)=1 ≤ 0.3*4=1.2

    def test_stale_prev_refreshes_from_old_score(self):
        prev_at = self.NOW - timedelta(hours=6, minutes=1)
        prev, rising = event_rising_state(1.0, prev_at, old_score=8.0, new_score=8.0, now=self.NOW)
        assert prev == 8.0  # 基准翻新为上一窗口末
        assert rising is False  # 本轮持平

    def test_stale_prev_rising_across_window(self):
        prev_at = self.NOW - timedelta(hours=7)
        prev, rising = event_rising_state(1.0, prev_at, old_score=1.0, new_score=8.0, now=self.NOW)
        assert prev == 1.0
        assert rising is True  # 跨窗口大涨

    def test_zero_baseline_needs_positive_gain(self):
        # 分母下限 1.0：0 基准需正向绝对增量 > 阈值，0→ε 噪声不触发
        prev, rising = event_rising_state(0.0, self.NOW - timedelta(hours=7), 0.0, 0.1, self.NOW)
        assert prev == 0.0
        assert rising is False  # 0.1 ≤ 0.3*1
        _, rising2 = event_rising_state(0.0, self.NOW - timedelta(hours=7), 0.0, 0.5, self.NOW)
        assert rising2 is True

    def test_naive_prev_at_treated_as_utc(self):
        naive = datetime(2026, 10, 12, 6, 0)  # naive
        prev, _ = event_rising_state(2.0, naive, 2.0, 2.0, self.NOW)
        assert prev == 2.0  # 不抛异常即可

    def test_threshold_custom(self):
        prev_at = self.NOW - timedelta(minutes=10)
        _, rising = event_rising_state(4.0, prev_at, 4.5, 5.0, self.NOW, threshold=0.2)
        assert rising is True  # (5-4)=1 > 0.2*4=0.8
