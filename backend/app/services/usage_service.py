"""Token 用量与成本计费服务。

record_usage 使用独立数据库会话落账：业务事务成败不影响已发生的调用计量，
记账自身失败也只记日志、绝不向调用方抛异常（不阻塞采集 / AI 流水线）。
费用在入库时按 model_price 当时单价计算（人民币元，6 位小数）：
    cost = (prompt - cached)/1M × 输入价 + cached/1M × 缓存价 + completion/1M × 输出价
未配置缓存价的模型缓存部分按输入价全价计（口径退化为"全量输入价"）；
无价目记录的模型记 token、cost=0。
"""
import inspect
import logging
import math
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import new_session
from app.models.article import Article
from app.models.usage import AiUsageLog, ModelPrice
from app.utils.tz import TZ_BJ, bj_now

logger = logging.getLogger(__name__)

# 降级 / 本地模型标识：非真实 API 调用（或本地自托管、无按量账单），不计入
# LLM 覆盖率分子、不进未计价盲区告警。
# extractive-textrank-v2 ↔ app/ai/summary.py；local-hash-256-v2 ↔ app/ai/embedding.py
# 的 LOCAL_EMBED_MODEL（此处用字面量，反向 import 会循环依赖）。
# bge-small-zh-v1.5 ↔ docker-compose.override.yml 的本地 embedding 服务
# （fastembed 自托管，EMBEDDING_BASE_URL=http://embedding:80/v1）：真实成本为 0
# 而非"未计价"，留在盲区清单会让面板长期挂假告警（2026-09-12 审计，库内 262 条
# 流水被误报）。若改用硅基流动等云端 embedding，把该行移除并在价目表配价即可。
# v1 旧名（extractive-textrank-v1 / local-hash-256）：改名前的历史流水仍在 30 天
# 窗口内，不入名单会把本地兜底当成真实 LLM 成功，覆盖率虚高（2026-09-10 审计，
# 实测 412 条 v1 流水被误计入分子）。
LOCAL_MODELS = {
    "extractive-textrank-v2", "extractive-textrank-v1",
    "local-hash-256-v2", "local-hash-256",
    "compose-facts",
    "BAAI/bge-small-zh-v1.5",
}

# CSV 导出首行注释（对账口径说明）
CSV_NOTE = (
    "本地口径=纯token成本，不含阶梯折扣/赠送额度/代金券/税点；"
    "缓存命中输入按 model_price.price_cache 计（未配置则按输入价全价计，会高于供应商账单），"
    "与供应商账单对账存在口径差异"
)

CSV_HEADERS = [
    "id", "created_at", "task_type", "trigger", "model", "status",
    "article_id", "article_title", "event_id",
    "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens",
    "latency_ms", "cost", "error",
]

# 明细流水允许的过滤值（API 层校验 pattern 由此拼装，勿在端点另写死一份）。
# conn_test：运维页「测试连接」按钮的真实 API 调用——同样烧 token，必须落账。
# article：文章草稿生成（article_gen_service）
LOG_TASK_TYPES = ("summary", "event_summary", "embedding", "report", "conn_test", "article", "site_article")
LOG_STATUSES = ("success", "parse_fail", "api_fail")

# 单价防呆上限（元/百万 token）。最贵商用模型也在千元级；超过即几乎必是
# 手滑多打零（如 2 写成 20000），在服务层拦下，防止之后每条流水按天价锁定。
MAX_MODEL_PRICE = 10_000


class BudgetExceededError(Exception):
    """当日 AI 成本已超预算且硬熔断开启（llm_client.LLMBudgetExceededError
    继承本类并叠加 LLMAPIError，调用方既有降级链自动接管）。"""

    def __init__(self, spent: float, budget: float):
        super().__init__(
            f"已达 AI 日预算硬上限：今日已计 ¥{spent:.2f} / 预算 ¥{budget:.2f}（北京时间日界，跨日自动恢复）"
        )
        self.spent = spent
        self.budget = budget


# —— 预算硬熔断速算（2026-10-12）：Redis 日累计计数，免每次 LLM 调用前 SUM 全表 ——

_DAY_KEY_TTL_SECONDS = 36 * 3600  # 跨过北京日界自然失效，36h 覆盖最大时区漂移


def _day_key(now: datetime | None = None) -> str:
    return "aicost:" + ((now or bj_now()).date().isoformat())


def bump_today_cost(cost_delta: float, now: datetime | None = None) -> None:
    """落账成功后同步累加 Redis 当日速算值。Redis 不可用静默跳过：
    熔断判定自动回退 ai_usage_log SQL 汇总（fail-open，与告警口径一致）。"""
    try:
        from app.services.infra_status import redis_client

        key = _day_key(now)
        pipe = redis_client().pipeline()
        pipe.incrbyfloat(key, float(cost_delta or 0.0))
        pipe.expire(key, _DAY_KEY_TTL_SECONDS)
        pipe.execute()
    except Exception:  # noqa: BLE001
        logger.debug("bump_today_cost skipped（Redis 不可用，熔断判定回退 SQL 汇总）", exc_info=True)


def today_spend_fast(db: Session, now: datetime | None = None) -> float:
    """当日已计成本速算（元）：Redis 计数优先；键缺失/Redis 不可用时回源
    ai_usage_log 汇总并回写计数（重建成后续请求的快路径）。"""
    from app.services.infra_status import redis_client

    key = _day_key(now)
    try:
        raw = redis_client().get(key)
        if raw is not None:
            return float(raw or 0.0)
    except Exception:  # noqa: BLE001
        pass
    spent = float(usage_day_summary(db, (now or bj_now()).date())["cost"] or 0.0)
    try:
        pipe = redis_client().pipeline()
        pipe.incrbyfloat(key, spent)
        pipe.expire(key, _DAY_KEY_TTL_SECONDS)
        pipe.execute()
    except Exception:  # noqa: BLE001
        pass
    return spent


def assert_budget_available(db: Session) -> None:
    """预算硬熔断前置检查（LLM 调用前，llm_client.chat 统一触发）。

    - 预算未配置（0）→ 直接放行（零开销路径，维持既有告警语义）；
    - 预算已配置但 ai_budget_hard_stop 关闭 → 放行（只告警不拦截）；
    - 超预算且开启 → 抛 BudgetExceededError（经 llm_client 包装为
      LLMAPIError 分型，摘要回退抽取式 / 草稿按失败落账）。
    """
    from app.services import config_service

    try:
        budget = float(config_service.get_effective(db, "ai_daily_cost_budget") or 0)
    except Exception:  # noqa: BLE001
        budget = 0.0
    if budget <= 0:
        return
    try:
        hard = bool(config_service.get_effective(db, "ai_budget_hard_stop"))
    except Exception:  # noqa: BLE001
        from app.core.config import get_settings

        hard = bool(getattr(get_settings(), "ai_budget_hard_stop", False))
    if not hard:
        return
    spent = today_spend_fast(db)
    if spent > budget:
        from app.ai.llm_client import LLMBudgetExceededError

        raise LLMBudgetExceededError(spent, budget)


def extract_openai_usage(body: dict) -> dict:
    """从 OpenAI 兼容响应体提取 usage 四项；缺失字段记 0。

    兼容两种缓存字段风格：
    - OpenAI / 智谱：usage.prompt_tokens_details.cached_tokens
    - DeepSeek：usage.prompt_cache_hit_tokens（prompt 拆 hit/miss 两段，二者之和=prompt）
    cached 一律钳制到 [0, prompt]，防御异常供应商返回。
    """
    u = (body or {}).get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    cached = int(details.get("cached_tokens") or 0) or int(u.get("prompt_cache_hit_tokens") or 0)
    cached = max(0, min(cached, prompt))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": int(u.get("total_tokens") or (prompt + completion)),
    }


def calc_cost(
    db: Session,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """按价目表计算费用（元，6 位小数）；无价目记录 → 0。

    缓存命中部分按 price_cache 计价（未配置则回落输入价=全价口径）；
    cached 钳制到 [0, prompt]，防止脏数据把输入段算成负数。
    """
    price = db.get(ModelPrice, model)
    if not price:
        return 0.0
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    cached = max(0, min(int(cached_tokens or 0), prompt))
    price_cache = float(price.price_cache) if price.price_cache is not None else float(price.price_input)
    cost = (
        (prompt - cached) / 1_000_000 * float(price.price_input)
        + cached / 1_000_000 * price_cache
        + completion / 1_000_000 * float(price.price_output)
    )
    return round(cost, 6)


def record_usage(
    task_type: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "success",
    error: str | None = None,
    article_id: int | None = None,
    event_id: int | None = None,
    trigger: str = "auto",
) -> None:
    """落一条 AI 调用流水（独立会话/事务；失败仅记日志，不影响业务）。

    total_tokens：调用方以 **usage 展开传入 extract_openai_usage 的四项时，
    优先采用服务端返回值；≤0 时回退 prompt+completion 自算。
    """
    try:
        db = new_session()
        try:
            total = int(total_tokens or 0) or (int(prompt_tokens or 0) + int(completion_tokens or 0))
            cost = calc_cost(db, model, prompt_tokens, completion_tokens, cached_tokens)
            db.add(AiUsageLog(
                task_type=task_type,
                trigger=trigger,
                model=model or "",
                article_id=article_id,
                event_id=event_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                total_tokens=total,
                latency_ms=int(latency_ms or 0),
                status=status,
                error=(str(error)[:2000] if error else None),
                cost=cost,
            ))
            db.commit()
        finally:
            db.close()
        # 预算硬熔断的 Redis 日累计：落账成功才累加；自身失败不影响业务
        bump_today_cost(cost)
    except Exception:
        logger.exception("record_usage failed (task_type=%s model=%s)", task_type, model)


def assert_record_usage_compatible() -> None:
    """签名回归哨兵：extract_openai_usage 的返回键必须能被 record_usage 接受。

    历史教训：extract 曾返回 total_tokens 而 record_usage 无此参数，所有
    **usage 展开调用在传参阶段即 TypeError（P0，详见 tests/test_usage.py）。
    """
    usage = extract_openai_usage({"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    inspect.signature(record_usage).bind(task_type="summary", model="m", **usage)


# ———————————————————— 价目表 CRUD ————————————————————

def list_prices(db: Session) -> list[ModelPrice]:
    """全部价目（按模型名排序）。"""
    return db.query(ModelPrice).order_by(ModelPrice.model).all()


def upsert_price(
    db: Session,
    model: str,
    price_input: float,
    price_output: float,
    price_cache: float | None = None,
) -> tuple[ModelPrice, bool]:
    """新增 / 更新某模型单价（每百万 token，元）。调价只影响之后的新记录。

    price_cache=None 表示清掉缓存价（回落输入价全价口径）。
    单价防呆：非有限值 / 负数 / 超过 MAX_MODEL_PRICE（几乎必为手滑多打零）
    抛 ValueError，由 API 层转 400。
    返回 (行, 是否新建)——供 API 区分"新增"与"改价"（提示语 + 审计 detail）。
    """
    for name, val in (("price_input", price_input), ("price_output", price_output),
                      ("price_cache", price_cache)):
        if val is None:
            continue
        if not math.isfinite(float(val)) or float(val) < 0:
            raise ValueError(f"{name} 必须为非负数值")
        if float(val) > MAX_MODEL_PRICE:
            raise ValueError(f"{name} 超过防呆上限 {MAX_MODEL_PRICE} 元/百万 token，疑似录入错误")
    row = db.get(ModelPrice, model)
    created = row is None
    try:
        if row is None:
            row = ModelPrice(model=model, price_input=price_input, price_output=price_output)
            db.add(row)
        else:
            row.price_input = price_input
            row.price_output = price_output
        row.price_cache = price_cache
        db.commit()
    except IntegrityError:
        # 并发双击「新增」：两个请求同时走到 INSERT，唯一键冲突的那个转更新路径重放一次，
        # 避免 500（价格后写者胜，与无并发时的最终态一致）
        db.rollback()
        row = db.get(ModelPrice, model)
        if row is None:
            raise
        created = False
        row.price_input = price_input
        row.price_output = price_output
        row.price_cache = price_cache
        db.commit()
    db.refresh(row)
    return row, created


def delete_price(db: Session, model: str) -> bool:
    """删除某模型价目（误录入的模型名得以移除；删除后盲区告警会重新提示，自愈可见）。
    返回是否存在（供 API 区分 404）。历史流水按入库时锁定的单价计，不受影响。"""
    row = db.get(ModelPrice, model)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def cleanup_expired_usage(db: Session, retention_days: int) -> int:
    """物理删除超过保留期的用量流水（周清理任务调用；0 = 永久保留，跳过）。
    长期归档先用 CSV 导出（/usage/export），删除不可逆。"""
    if not retention_days or retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = (
        db.query(AiUsageLog)
        .filter(AiUsageLog.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


def price_dict(p: ModelPrice) -> dict:
    return {
        "model": p.model,
        "price_input": float(p.price_input),
        "price_output": float(p.price_output),
        "price_cache": float(p.price_cache) if p.price_cache is not None else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def csv_safe_cell(value) -> str:
    """CSV 公式注入防护：以 = + - @ 或制表/回车/换行开头的单元格加 ' 前缀，
    防 Excel/WPS 当公式执行（OWASP CSV Injection 惯例）。非字符串原样转 str。"""
    s = "" if value is None else str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + s
    return s


# ———————————————————— 聚合查询（供 API / 日报） ————————————————————

def _period_start(period: str, now: datetime) -> tuple[datetime, str]:
    """period → 起始时间（today/month 按北京时间自然日切分）。"""
    if period == "7d":
        return now - timedelta(days=7), "7d"
    if period == "30d":
        return now - timedelta(days=30), "30d"
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "month"
    return now.replace(hour=0, minute=0, second=0, microsecond=0), "today"


def _group_stats(q, col) -> list[dict]:
    """按指定列分组统计：调用数 / token / 成本。"""
    rows = (
        q.with_entities(
            col,
            func.count(),
            func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
            func.coalesce(func.sum(AiUsageLog.cost), 0),
        )
        .group_by(col)
        .all()
    )
    return [
        {"key": r[0], "calls": int(r[1]), "tokens": int(r[2] or 0), "cost": round(float(r[3] or 0), 6)}
        for r in rows
    ]


def _bj_day_expr(db: Session):
    """按北京时间日界取日的可移植表达式。

    PG 显式转换到 Asia/Shanghai，避免随会话时区漂移；SQLite 测试库把
    DateTime 按 UTC naive 保存，因此先加 8 小时再取日期。
    """
    if db.get_bind().dialect.name == "postgresql":
        return func.date(func.timezone("Asia/Shanghai", AiUsageLog.created_at))
    return func.date(AiUsageLog.created_at, "+8 hours")


def _db_time(db: Session, value: datetime) -> datetime:
    """将带时区边界转换为 SQLite 测试库使用的 UTC naive 时间。"""
    if db.get_bind().dialect.name == "sqlite" and value.tzinfo:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def usage_summary(
    db: Session,
    period: str = "today",
    include_daily: bool = True,
    include_groups: bool = True,
) -> dict:
    """用量/成本看板聚合：总量、分组、覆盖率、失败数、近 30 天按日趋势。

    include_groups：短周期请求可关掉分组统计（前端仅 30d 视图消费）；
    关闭时 by_task_type/by_model 返回空列表（响应形状不变）。
    include_daily：关掉 30 天日趋势。日趋势恒为 30 个整北京时间日桶（缺数据日补零），
    与窗口起点取整到日界，图形不再把无调用日误读成缺数据。

    unpriced_models 与分组无关、恒返回：近窗口有调用但价目表没配单价、且非
    本地降级模型的模型（本地模型 cost=0 是设计使然）——账面成本被低估的盲区告警。
    """
    now = bj_now()
    start, period = _period_start(period, now)
    query_start = _db_time(db, start)

    q = db.query(AiUsageLog).filter(AiUsageLog.created_at >= query_start)

    # 单次条件聚合算完总量与全部健康指标（此前是 6 次独立窗口扫描；看板每次刷新
    # 调 today/month/30d 三次本接口，扫描次数直接 ×3）。响应语义与拆分版完全一致。
    is_summary = AiUsageLog.task_type == "summary"
    totals = q.with_entities(
        func.count(),
        func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
        func.coalesce(func.sum(AiUsageLog.cost), 0),
        func.coalesce(func.sum(AiUsageLog.cached_tokens), 0),
        func.coalesce(func.sum(AiUsageLog.prompt_tokens), 0),
        # 摘要总数（LLM 覆盖率分母）
        func.coalesce(func.sum(case((is_summary, 1), else_=0)), 0),
        # LLM 摘要成功数（覆盖率分子；本地降级模型与失败都不计入）
        func.coalesce(func.sum(case((
            and_(is_summary, AiUsageLog.status == "success", AiUsageLog.model.notin_(LOCAL_MODELS)),
            1,
        ), else_=0)), 0),
        # Embedding API 失败数
        func.coalesce(func.sum(case((
            and_(AiUsageLog.task_type == "embedding", AiUsageLog.status == "api_fail"), 1,
        ), else_=0)), 0),
    ).one()
    total_calls = int(totals[0])
    summary_total = int(totals[5])
    llm_ok = int(totals[6])
    embedding_api_fail = int(totals[7])

    # 模型分组恒算一份：by_model 与 unpriced_models 共用这一次扫描
    model_groups = _group_stats(q, AiUsageLog.model)
    priced = {p.model for p in list_prices(db)}
    unpriced = [
        g for g in model_groups
        if g["key"] and g["key"] not in priced and g["key"] not in LOCAL_MODELS
    ]

    event_calls_24h = (
        db.query(func.count(AiUsageLog.id))
        .filter(
            AiUsageLog.task_type == "event_summary",
            AiUsageLog.created_at >= _db_time(db, now - timedelta(hours=24)),
        )
        .scalar() or 0
    )

    daily: list[dict] = []
    if include_daily:
        # 近 30 个整北京时间日（含今天）：起点取整到日界，缺失日补零
        day_start = (now - timedelta(days=29)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        bj_day = _bj_day_expr(db)
        daily_rows = (
            db.query(
                bj_day,
                func.count(),
                func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                func.coalesce(func.sum(AiUsageLog.cost), 0),
            )
            .filter(AiUsageLog.created_at >= _db_time(db, day_start))
            .group_by(bj_day)
            .all()
        )
        by_date = {str(r[0]): r for r in daily_rows}
        for i in range(30):
            d = (day_start + timedelta(days=i)).date().isoformat()
            r = by_date.get(d)
            daily.append({
                "date": d,
                "calls": int(r[1]) if r else 0,
                "tokens": int(r[2] or 0) if r else 0,
                "cost": round(float(r[3] or 0), 6) if r else 0.0,
            })

    return {
        "period": period,
        "start": start.isoformat(),
        "total_calls": total_calls,
        "total_tokens": int(totals[1] or 0),
        "total_cost": round(float(totals[2] or 0), 6),
        "total_cached": int(totals[3] or 0),
        "total_prompt": int(totals[4] or 0),
        "by_task_type": _group_stats(q, AiUsageLog.task_type) if include_groups else [],
        "by_model": model_groups if include_groups else [],
        "unpriced_models": unpriced,
        "summary_total": summary_total,
        "llm_coverage": round(llm_ok / summary_total * 100, 1) if summary_total else 0,
        "embedding_api_fail": embedding_api_fail,
        "event_summary_calls_24h": int(event_calls_24h),
        "daily": daily,
    }


def usage_window_summary(db: Session, start: datetime, end: datetime) -> dict:
    """按左闭右开时间窗口汇总调用 / token / 成本。

    调用方负责决定时间窗口的业务时区；日报使用北京时间自然日窗口，
    成本看板和预算告警使用北京时间日界；数据库仍按 UTC 时刻保存。
    """
    row = (
        db.query(
            func.count(),
            func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
            func.coalesce(func.sum(AiUsageLog.cost), 0),
        )
        .filter(
            AiUsageLog.created_at >= _db_time(db, start),
            AiUsageLog.created_at < _db_time(db, end),
        )
        .one()
    )
    return {
        "calls": int(row[0] or 0),
        "tokens": int(row[1] or 0),
        "cost": round(float(row[2] or 0), 6),
    }


def usage_day_summary(db: Session, day: date_type) -> dict:
    """某北京时间自然日（00:00-24:00）的调用 / token / 成本汇总。"""
    start = datetime(day.year, day.month, day.day, tzinfo=TZ_BJ)
    end = start + timedelta(days=1)
    return usage_window_summary(db, start, end)


def clear_usage_logs(db: Session) -> int:
    """清空全部 AI 调用流水，返回删除行数。

    破坏性、不可恢复：仅供管理端「清空流水」按钮，调用方必须单独审计
    （admin.usage_logs_cleared，记录删除行数）。价目表（model_price）不受影响；
    常规清理仍走 auth_cleanup_task 的保留期机制（ai_usage_retention_days）。
    """
    deleted = db.query(AiUsageLog).delete(synchronize_session=False)
    db.commit()
    return deleted


def usage_logs(
    db: Session,
    page: int = 1,
    page_size: int = 50,
    task_type: str | None = None,
    status: str | None = None,
    model: str | None = None,
) -> dict:
    """流水明细分页（倒序），联 article 取标题；支持按任务类型/状态/模型过滤。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    q = db.query(AiUsageLog).order_by(AiUsageLog.id.desc())
    if task_type:
        q = q.filter(AiUsageLog.task_type == task_type)
    if status:
        q = q.filter(AiUsageLog.status == status)
    if model:
        q = q.filter(AiUsageLog.model == model)
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()

    art_ids = [r.article_id for r in rows if r.article_id]
    titles = {}
    if art_ids:
        titles = {a.id: a.title for a in db.query(Article).filter(Article.id.in_(art_ids)).all()}

    return {
        "items": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "task_type": r.task_type,
                "trigger": r.trigger,
                "model": r.model,
                "status": r.status,
                "article_id": r.article_id,
                "article_title": titles.get(r.article_id),
                "event_id": r.event_id,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cached_tokens": r.cached_tokens,
                "total_tokens": r.total_tokens,
                "latency_ms": r.latency_ms,
                "cost": float(r.cost or 0),
                "error": (r.error[:200] if r.error else None),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
