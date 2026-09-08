"""Token 用量与成本计费服务。

record_usage 使用独立数据库会话落账：业务事务成败不影响已发生的调用计量，
记账自身失败也只记日志、绝不向调用方抛异常（不阻塞采集 / AI 流水线）。
费用在入库时按 model_price 当时单价计算：cost = token/1M × 单价（人民币元，6 位小数），
无价目记录的模型记 token、cost=0。
"""
import logging
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import new_session
from app.models.article import Article
from app.models.usage import AiUsageLog, ModelPrice

logger = logging.getLogger(__name__)

# 降级 / 本地模型标识：非真实 API 调用，不计入 LLM 覆盖率分子。
# extractive-textrank-v2 ↔ app/ai/summary.py；local-hash-256-v2 ↔ app/ai/embedding.py
# 的 LOCAL_EMBED_MODEL（此处用字面量，反向 import 会循环依赖）——改名需两处同步。
LOCAL_MODELS = {"extractive-textrank-v2", "local-hash-256-v2", "compose-facts"}

# CSV 导出首行注释（对账口径说明）
CSV_NOTE = "本地口径=纯token成本，不含阶梯折扣/赠送额度/代金券/税点，与供应商账单对账存在口径差异"

CSV_HEADERS = [
    "id", "created_at", "task_type", "trigger", "model", "status",
    "article_id", "article_title", "event_id",
    "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens",
    "latency_ms", "cost", "error",
]


def extract_openai_usage(body: dict) -> dict:
    """从 OpenAI 兼容响应体提取 usage 四项；缺失字段记 0。"""
    u = (body or {}).get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "total_tokens": int(u.get("total_tokens") or (prompt + completion)),
    }


def calc_cost(db: Session, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按价目表计算费用（元，6 位小数）；无价目记录 → 0。"""
    price = db.get(ModelPrice, model)
    if not price:
        return 0.0
    cost = (
        prompt_tokens / 1_000_000 * float(price.price_input)
        + completion_tokens / 1_000_000 * float(price.price_output)
    )
    return round(cost, 6)


def record_usage(
    task_type: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "success",
    error: str | None = None,
    article_id: int | None = None,
    event_id: int | None = None,
    trigger: str = "auto",
) -> None:
    """落一条 AI 调用流水（独立会话/事务；失败仅记日志，不影响业务）。"""
    try:
        db = new_session()
        try:
            db.add(AiUsageLog(
                task_type=task_type,
                trigger=trigger,
                model=model or "",
                article_id=article_id,
                event_id=event_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                latency_ms=int(latency_ms or 0),
                status=status,
                error=(str(error)[:2000] if error else None),
                cost=calc_cost(db, model, prompt_tokens, completion_tokens),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("record_usage failed (task_type=%s model=%s)", task_type, model)


# ———————————————————— 价目表 CRUD ————————————————————

def list_prices(db: Session) -> list[ModelPrice]:
    """全部价目（按模型名排序）。"""
    return db.query(ModelPrice).order_by(ModelPrice.model).all()


def upsert_price(db: Session, model: str, price_input: float, price_output: float) -> ModelPrice:
    """新增 / 更新某模型单价（每百万 token，元）。调价只影响之后的新记录。"""
    row = db.get(ModelPrice, model)
    if row is None:
        row = ModelPrice(model=model, price_input=price_input, price_output=price_output)
        db.add(row)
    else:
        row.price_input = price_input
        row.price_output = price_output
    db.commit()
    db.refresh(row)
    return row


def price_dict(p: ModelPrice) -> dict:
    return {
        "model": p.model,
        "price_input": float(p.price_input),
        "price_output": float(p.price_output),
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# ———————————————————— 聚合查询（供 API / 日报） ————————————————————

def _period_start(period: str, now: datetime) -> tuple[datetime, str]:
    """period → 起始时间（today 按 UTC 0 点起算，与日报日期口径一致）。"""
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


def usage_summary(db: Session, period: str = "today") -> dict:
    """用量/成本看板聚合：总量、分组、覆盖率、失败数、近 30 天按日趋势。"""
    now = datetime.now(timezone.utc)
    start, period = _period_start(period, now)

    q = db.query(AiUsageLog).filter(AiUsageLog.created_at >= start)
    total_calls = q.count()
    totals = q.with_entities(
        func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
        func.coalesce(func.sum(AiUsageLog.cost), 0),
    ).one()

    # LLM 覆盖率 = LLM 摘要成功数 / 总摘要数（降级与失败都拉低覆盖率）
    summary_total = q.filter(AiUsageLog.task_type == "summary").count()
    llm_ok = q.filter(
        AiUsageLog.task_type == "summary",
        AiUsageLog.status == "success",
        AiUsageLog.model.notin_(LOCAL_MODELS),
    ).count()

    embedding_api_fail = q.filter(
        AiUsageLog.task_type == "embedding", AiUsageLog.status == "api_fail"
    ).count()
    event_calls_24h = (
        db.query(func.count(AiUsageLog.id))
        .filter(
            AiUsageLog.task_type == "event_summary",
            AiUsageLog.created_at >= now - timedelta(hours=24),
        )
        .scalar() or 0
    )

    # 近 30 天按日趋势（UTC 日界，与 today 口径一致）
    utc_day = func.date(func.timezone("UTC", AiUsageLog.created_at))
    daily_rows = (
        db.query(
            utc_day,
            func.count(),
            func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
            func.coalesce(func.sum(AiUsageLog.cost), 0),
        )
        .filter(AiUsageLog.created_at >= now - timedelta(days=30))
        .group_by(utc_day)
        .order_by(utc_day)
        .all()
    )

    return {
        "period": period,
        "start": start.isoformat(),
        "total_calls": total_calls,
        "total_tokens": int(totals[0] or 0),
        "total_cost": round(float(totals[1] or 0), 6),
        "by_task_type": _group_stats(q, AiUsageLog.task_type),
        "by_model": _group_stats(q, AiUsageLog.model),
        "summary_total": summary_total,
        "llm_coverage": round(llm_ok / summary_total * 100, 1) if summary_total else 0,
        "embedding_api_fail": embedding_api_fail,
        "event_summary_calls_24h": int(event_calls_24h),
        "daily": [
            {
                "date": str(r[0]),
                "calls": int(r[1]),
                "tokens": int(r[2] or 0),
                "cost": round(float(r[3] or 0), 6),
            }
            for r in daily_rows
        ],
    }


def usage_day_summary(db: Session, day: date_type) -> dict:
    """某自然日（UTC 0-24 点，与日报日期口径一致）的调用 / token / 成本汇总。"""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    row = (
        db.query(
            func.count(),
            func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
            func.coalesce(func.sum(AiUsageLog.cost), 0),
        )
        .filter(AiUsageLog.created_at >= start, AiUsageLog.created_at < end)
        .one()
    )
    return {
        "calls": int(row[0] or 0),
        "tokens": int(row[1] or 0),
        "cost": round(float(row[2] or 0), 6),
    }


def usage_logs(db: Session, page: int = 1, page_size: int = 50) -> dict:
    """流水明细分页（倒序），联 article 取标题。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    q = db.query(AiUsageLog).order_by(AiUsageLog.id.desc())
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
