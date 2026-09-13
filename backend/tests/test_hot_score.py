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
