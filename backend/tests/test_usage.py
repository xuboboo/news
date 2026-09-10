"""成本看板计量域单测（usage_service）。

P0 回归教训：extract_openai_usage 曾返回 total_tokens 而 record_usage 无此参数，
所有 **usage 展开调用在传参阶段即 TypeError——真实 API 模式下整条 AI 流水线瘫痪。
test_record_usage_accepts_extracted_usage 即该缺陷的永久哨兵。

DB 相关测试用 SQLite 内存库，仅建 AiUsageLog/ModelPrice 两张表
（article 含 pgvector Vector 列，SQLite 建不了）；日趋势的 UTC 日桶表达式已做
方言自适应（PG=timezone('UTC', ts)，SQLite=date()），include_daily=True 可直接测
零填充整日桶（见 test_usage_daily_zero_fill）。
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.usage import AiUsageLog, ModelPrice
from app.services.usage_service import (
    MAX_MODEL_PRICE,
    CSV_HEADERS,
    calc_cost,
    cleanup_expired_usage,
    csv_safe_cell,
    delete_price,
    extract_openai_usage,
    list_prices,
    price_dict,
    upsert_price,
    usage_day_summary,
    usage_logs,
    usage_summary,
)


# ———————————————————— P0 回归哨兵 ————————————————————

def test_record_usage_accepts_extracted_usage():
    """extract_openai_usage 返回的每一项都必须能被 record_usage 接受（**usage 展开兼容）。"""
    for body in (
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 5,
                   "prompt_tokens_details": {"cached_tokens": 4}, "total_tokens": 15}},
        {},  # 无 usage → 全 0 四项
    ):
        usage = extract_openai_usage(body)
        # bind() 在参数不匹配时抛 TypeError——正是 P0 的爆炸点
        inspect.signature(_record_usage_ref()).bind(task_type="summary", model="m", **usage)


def test_local_models_cover_legacy_names():
    """v1 时代的本地兜底模型名（改名前的历史流水）必须计入本地名单，
    否则抽取式摘要被当成真实 LLM 成功，覆盖率分子虚高（2026-09-10 实测 412 条）。"""
    from app.services.usage_service import LOCAL_MODELS

    assert {"extractive-textrank-v1", "local-hash-256"} <= LOCAL_MODELS


def _record_usage_ref():
    from app.services.usage_service import record_usage
    return record_usage


# ———————————————————— extract_openai_usage ————————————————————

def test_extract_openai_style_with_cache():
    u = extract_openai_usage({"usage": {
        "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
        "prompt_tokens_details": {"cached_tokens": 40},
    }})
    assert u == {"prompt_tokens": 100, "completion_tokens": 30, "cached_tokens": 40, "total_tokens": 130}


def test_extract_deepseek_style_cache():
    """DeepSeek 风格：prompt_cache_hit_tokens（无 prompt_tokens_details）。"""
    u = extract_openai_usage({"usage": {
        "prompt_tokens": 100, "completion_tokens": 30,
        "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40,
    }})
    assert u["cached_tokens"] == 60
    assert u["total_tokens"] == 130  # 缺 total → prompt+completion


def test_extract_missing_and_clamp():
    assert extract_openai_usage({}) == {
        "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
    }
    assert extract_openai_usage(None)["total_tokens"] == 0
    # 脏数据：cached > prompt → 钳制到 prompt
    u = extract_openai_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 0,
                                        "prompt_tokens_details": {"cached_tokens": 99}}})
    assert u["cached_tokens"] == 10


# ———————————————————— calc_cost（SQLite） ————————————————————

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    AiUsageLog.__table__.create(engine)
    ModelPrice.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def test_calc_cost_no_price(db):
    assert calc_cost(db, "unknown-model", 1000, 1000) == 0.0


def test_calc_cost_full_price(db):
    upsert_price(db, "m1", price_input=2.0, price_output=8.0)
    # 1M 输入 + 0.5M 输出 = 2 + 4 = 6 元
    assert calc_cost(db, "m1", 1_000_000, 500_000) == 6.0


def test_calc_cost_with_cache_price(db):
    upsert_price(db, "m2", price_input=4.0, price_output=16.0, price_cache=1.0)
    # (1M-0.6M)×4 + 0.6M×1 + 0 = 1.6 + 0.6 = 2.2 元
    assert calc_cost(db, "m2", 1_000_000, 0, cached_tokens=600_000) == pytest.approx(2.2)


def test_calc_cost_cache_fallback_to_input_price(db):
    """未配置缓存价 → 缓存部分按输入价全价计（口径退化为全量输入价）。"""
    upsert_price(db, "m3", price_input=4.0, price_output=0.0, price_cache=None)
    assert calc_cost(db, "m3", 1_000_000, 0, cached_tokens=600_000) == pytest.approx(4.0)


def test_calc_cost_clamps_dirty_cache(db):
    upsert_price(db, "m4", price_input=1.0, price_output=0.0, price_cache=0.5)
    # cached=2M > prompt=1M → 钳制到 1M：(1M-1M)×1 + 1M×0.5 = 0.5
    assert calc_cost(db, "m4", 1_000_000, 0, cached_tokens=2_000_000) == pytest.approx(0.5)


def test_calc_cost_rounding_6(db):
    upsert_price(db, "m5", price_input=3.7, price_output=0.0)
    assert calc_cost(db, "m5", 7, 0) == round(7 / 1_000_000 * 3.7, 6)


# ———————————————————— 价目 CRUD ————————————————————

def test_upsert_price_create_update_and_created_flag(db):
    row, created = upsert_price(db, "mx", 1.0, 2.0, 0.5)
    assert created is True
    row2, created2 = upsert_price(db, "mx", 3.0, 4.0, None)
    assert created2 is False
    assert row2.price_input == 3.0
    assert row2.price_cache is None  # None = 清除缓存价
    d = price_dict(row2)
    assert d["price_cache"] is None
    assert set(CSV_HEADERS) >= {"cached_tokens", "cost"}  # 导出列含缓存与成本
    assert [p.model for p in list_prices(db)] == ["mx"]


def test_delete_price(db):
    """删除存在的价目 → True 且行消失；再删 → False（API 据此 404）。"""
    upsert_price(db, "del-me", 1.0, 2.0)
    assert delete_price(db, "del-me") is True
    assert list_prices(db) == []
    assert delete_price(db, "del-me") is False


def test_cleanup_expired_usage(db):
    """保留期清理：只删超期行；0/负数 = 永久保留，跳过。"""
    now = datetime.now(timezone.utc)
    _add_log(db, created_at=now - timedelta(days=400), model="old-model")
    _add_log(db, created_at=now - timedelta(days=1), model="new-model")

    assert cleanup_expired_usage(db, 0) == 0   # 0 = 永久保留
    assert cleanup_expired_usage(db, -5) == 0
    assert usage_logs(db)["total"] == 2

    assert cleanup_expired_usage(db, 365) == 1
    remaining = usage_logs(db)
    assert remaining["total"] == 1
    assert remaining["items"][0]["model"] == "new-model"
    assert cleanup_expired_usage(db, 365) == 0  # 幂等


def test_upsert_price_sanitizes_bad_values(db):
    """防呆：负数 / 非有限值 / 超上限（手滑多打零）一律 ValueError，不得落库。"""
    for kwargs in (
        {"price_input": -1.0, "price_output": 2.0},
        {"price_input": float("nan"), "price_output": 2.0},
        {"price_input": 1.0, "price_output": float("inf")},
        {"price_input": 1.0, "price_output": 2.0, "price_cache": MAX_MODEL_PRICE + 0.01},
    ):
        with pytest.raises(ValueError):
            upsert_price(db, "bad", **kwargs)
    assert list_prices(db) == []  # 校验失败不留半截行


# ———————————————————— 聚合与明细（SQLite，include_daily=False） ————————————————————

def _add_log(db, **kw):
    defaults = dict(
        task_type="summary", trigger="auto", model="glm-4-flash",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        prompt_tokens=100, completion_tokens=50, cached_tokens=0, total_tokens=150,
        latency_ms=800, status="success", cost=0.001,
    )
    defaults.update(kw)
    db.add(AiUsageLog(**defaults))
    db.commit()


def test_usage_summary_totals_coverage_groups(db):
    _add_log(db, task_type="summary", model="glm-4-flash", total_tokens=150, cost=0.1,
             cached_tokens=50, status="success")
    _add_log(db, task_type="summary", model="extractive-textrank-v2", total_tokens=0, cost=0,
             status="success")
    _add_log(db, task_type="summary", status="parse_fail", total_tokens=120, cost=0.05)
    _add_log(db, task_type="embedding", model="local-hash-256-v2", total_tokens=0, cost=0)
    _add_log(db, task_type="embedding", status="api_fail", total_tokens=200, cost=0.02,
             created_at=datetime.now(timezone.utc) - timedelta(hours=2))

    s = usage_summary(db, "today", include_daily=False)
    assert s["total_calls"] == 5
    assert s["total_tokens"] == 470
    assert s["total_cached"] == 50
    assert s["total_prompt"] == 500  # 缓存命中率分母
    assert s["total_cost"] == pytest.approx(0.17, abs=1e-9)
    assert s["summary_total"] == 3
    assert s["llm_coverage"] == pytest.approx(33.3, abs=0.1)  # 仅 1 次 LLM 成功
    assert s["embedding_api_fail"] == 1
    assert s["daily"] == []  # 关闭日趋势：形状保留、值为空
    by_task = {g["key"]: g["calls"] for g in s["by_task_type"]}
    assert by_task == {"summary": 3, "embedding": 2}
    by_model = {g["key"]: g["calls"] for g in s["by_model"]}
    assert by_model == {"glm-4-flash": 3, "extractive-textrank-v2": 1, "local-hash-256-v2": 1}


def test_usage_period_boundaries(db):
    """today 只含 UTC 0 点后；month 从 1 号起算。"""
    now = datetime.now(timezone.utc)
    _add_log(db, created_at=now - timedelta(hours=1))                       # 今天
    _add_log(db, created_at=now - timedelta(days=2), cost=0.5)              # 前天
    assert usage_summary(db, "today", include_daily=False)["total_calls"] == 1
    assert usage_summary(db, "30d", include_daily=False)["total_calls"] == 2
    month_calls = usage_summary(db, "month", include_daily=False)["total_calls"]
    assert month_calls in (1, 2)  # 取决于"现在"是否月初 48h 内，只验证不炸且口径 >= today


def test_usage_summary_unpriced_models(db):
    """未计价模型盲区：API 模型没配价必须告警；本地降级模型与空模型名不算。"""
    _add_log(db, model="glm-4-flash", prompt_tokens=100, total_tokens=150, cost=0.1)
    upsert_price(db, "glm-4-flash", 2.0, 8.0)
    _add_log(db, model="mystery-llm", prompt_tokens=200, total_tokens=300, cost=0.0)
    _add_log(db, model="local-hash-256-v2", prompt_tokens=0, total_tokens=0, cost=0.0,
             task_type="embedding")

    s = usage_summary(db, "today", include_daily=False)
    keys = [g["key"] for g in s["unpriced_models"]]
    assert keys == ["mystery-llm"]
    assert s["unpriced_models"][0]["calls"] == 1
    assert s["unpriced_models"][0]["tokens"] == 300


def test_usage_daily_zero_fill(db):
    """日趋势恒为 30 个整 UTC 日桶：缺失日补零、有数日聚合、末桶=今天。"""
    # 锚定 UTC 正午：now-1h/2h 恒落在今天，避免 UTC 0-1 点跑测跨日
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    _add_log(db, total_tokens=150, cost=0.1, created_at=now - timedelta(hours=1))
    _add_log(db, total_tokens=150, cost=0.1, created_at=now - timedelta(hours=2))
    _add_log(db, total_tokens=90, cost=0.3, created_at=now - timedelta(days=3, hours=1))

    s = usage_summary(db, "30d", include_daily=True, include_groups=False)
    days = s["daily"]
    assert len(days) == 30
    # 日期连续且最后一桶是今天（UTC）
    assert days[-1]["date"] == now.date().isoformat()
    expected_dates = {
        (now - timedelta(days=i)).date().isoformat() for i in range(30)
    }
    assert {d["date"] for d in days} == expected_dates
    by_date = {d["date"]: d for d in days}
    assert by_date[days[-1]["date"]]["calls"] == 2
    assert by_date[days[-1]["date"]]["tokens"] == 300
    assert by_date[(now - timedelta(days=3)).date().isoformat()]["tokens"] == 90
    # 无调用日补零
    empty = [d for d in days if d["calls"] == 0]
    assert len(empty) == 28
    assert all(d["tokens"] == 0 and d["cost"] == 0 for d in empty)


def test_usage_day_summary(db):
    # 锚定当日 UTC 正午造数：now - 5h 会在 UTC 05 点前跑测时跨到昨天（按自然日切分）
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    day = noon.date()
    _add_log(db, cost=0.25, created_at=noon)
    _add_log(db, cost=0.25, created_at=noon - timedelta(hours=5))
    r = usage_day_summary(db, day)
    assert r["calls"] == 2
    assert r["tokens"] == 300
    assert r["cost"] == pytest.approx(0.5, abs=1e-9)


def test_usage_logs_filters_and_pagination(db):
    _add_log(db)
    _add_log(db, status="api_fail", task_type="embedding", model="text-embedding-3")
    _add_log(db, status="api_fail", task_type="embedding", model="text-embedding-3",
             created_at=datetime.now(timezone.utc) - timedelta(days=40))  # 40 天前，30d 窗外不影响 logs

    full = usage_logs(db)
    assert full["total"] == 3
    assert len(full["items"]) == 3

    only_fail = usage_logs(db, status="api_fail")
    assert only_fail["total"] == 2
    assert all(i["status"] == "api_fail" for i in only_fail["items"])

    both = usage_logs(db, task_type="embedding", status="api_fail")
    assert both["total"] == 2
    only_summary = usage_logs(db, task_type="summary")
    assert only_summary["total"] == 1

    paged = usage_logs(db, page=1, page_size=2)
    assert paged["page_size"] == 2
    assert len(paged["items"]) == 2
    assert usage_logs(db, page=99)["items"] == []  # 越界页为空不炸


# ———————————————————— CSV 防注入 ————————————————————

@pytest.mark.parametrize("raw,expected", [
    ("=SUM(A1)", "'=SUM(A1)"),
    ("+1-1", "'+1-1"),
    ("-正常标题", "'-正常标题"),
    ("@cmd", "'@cmd"),
    ("\tTAB", "'\tTAB"),
    ("\rCR", "'\rCR"),
    ("\n=cmd", "'\n=cmd"),
    ("普通标题", "普通标题"),
    (None, ""),
    (123, "123"),
])
def test_csv_safe_cell(raw, expected):
    assert csv_safe_cell(raw) == expected
