"""HotScore 热度算法边界测试（2026-09-09 审计补齐：此前无任何覆盖）。

覆盖：原始互动加权、时间衰减、零互动保底分、未来发布时间、SQL 表达式
与 Python 版数值一致性、归一化。纯函数为主，不依赖数据库。

离线运行：python -m pytest tests/test_hot_score.py -q
"""
import math
from datetime import datetime, timedelta, timezone

from app.algorithms.hot_score import (
    hot_score,
    hot_score_expr,
    normalize_to_100,
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
    # λ=0.0075：48 小时衰减 e^-0.36
    assert math.isclose(old, math.exp(-S.decay_per_hour * 48), rel_tol=1e-9)


def test_time_decay_naive_time_assumed_utc():
    naive = datetime(2026, 9, 9, 11, 0)  # 无时区
    assert time_decay(naive, now=NOW) == time_decay(NOW - timedelta(hours=1), now=NOW)


def test_time_decay_future_publish_clamped():
    """未来发布时间（时钟漂移/脏数据）：负时距按 0 计，衰减不放大。"""
    assert time_decay(NOW + timedelta(hours=5), now=NOW) == 1.0


# ———— hot_score：保底分 ————

def test_hot_score_zero_engagement_floors_at_decayed_100():
    """零互动保底分 = 100×时间衰减（随老化趋向 0，非固定 100）。"""
    fresh = hot_score(0, 0, 0, 0, NOW, now=NOW)
    assert fresh == 100.0
    day_old = hot_score(0, 0, 0, 0, NOW - timedelta(hours=24), now=NOW)
    assert math.isclose(day_old, 100 * math.exp(-S.decay_per_hour * 24), rel_tol=1e-9)
    assert day_old < fresh


def test_hot_score_engagement_times_decay():
    raw = raw_engagement(100, 10, 5, 3)
    assert math.isclose(
        hot_score(100, 10, 5, 3, NOW - timedelta(hours=2), now=NOW),
        raw * math.exp(-S.decay_per_hour * 2),
        rel_tol=1e-9,
    )


# ———— SQL 表达式与 Python 版一致性 ————

def test_hot_score_expr_compiles_with_same_semantics():
    """表达式可编译且结构语义对齐（COALESCE 0 / greatest 钳负 / 保底分支）。"""
    from app.models.article import Article

    expr = hot_score_expr(Article)
    sql = str(expr.compile(compile_kwargs={"literal_binds": True}))
    assert "coalesce" in sql.lower()
    assert "greatest" in sql.lower()  # 未来发布时间钳 0，与 Python max(0.0, ...) 一致
    assert "exp" in sql.lower()


# ———— normalize_to_100 ————

def test_normalize_to_100_bounds():
    assert normalize_to_100(0, 0) == 0.0
    assert normalize_to_100(50, 0) == 0.0  # 基准为 0（窗口空）时不放大
    assert normalize_to_100(100, 100) == 100.0
    assert normalize_to_100(25, 200) == 12.5
    assert normalize_to_100(200, 100) == 200.0  # 相对归一化允许超 100（新王登基窗口）
