"""管理面板 API（运维监控 + 运行时配置）。

鉴权双通道（require_admin）：
① super_admin 登录会话（Cookie，前端常规路径）；
② X-Admin-Token 应急后门——与生效 admin_token 一致即放行，供脚本/回归使用；
ADMIN_NO_AUTH=true 时直接放行（仅限内网，公网必须关闭）。
"""
import csv
import hmac
import io
import time
from datetime import datetime, timedelta, timezone

import httpx
import redis as redis_lib
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import sessions as session_store
from app.auth.deps import current_user_of
from app.core.config import get_settings
from app.core.db import get_db
from app.models.article import Article
from app.models.event import HotEvent
from app.models.summary import ArticleSummary
from app.models.task_run import TaskRun
from app.models.usage import AiUsageLog
from app.models.user import ROLE_SUPER_ADMIN, User
from app.services import audit_service, config_service, usage_service
from app.services.embedding_service import get_embedding_fingerprint
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

router = APIRouter(prefix="/api/v2/admin")


# ———————————————————— 鉴权 ————————————————————

def require_admin(
    request: Request,
    db: Session = Depends(get_db),
    x_admin_token: str | None = Header(default=None),
):
    if get_settings().admin_no_auth:
        return  # 内网免鉴权模式（ADMIN_NO_AUTH=true）
    # ① 登录会话（super_admin）
    row = session_store.get_active_session(db, request.cookies.get(session_store.SESSION_COOKIE, ""))
    if row is not None:
        user = db.get(User, row.user_id)
        if user is not None and user.status == "active":
            if user.role != ROLE_SUPER_ADMIN:
                raise HTTPException(403, "需要平台超管权限")
            session_store.touch(row)
            if getattr(row, "_touched", False):
                db.commit()
            request.state.current_user = user
            request.state.current_session = row
            return
    # ② X-Admin-Token 应急后门（脚本/回归；未配置保持原 503 提示）
    effective = config_service.get_effective(db, "admin_token")
    if not effective:
        raise HTTPException(
            503,
            "admin_token 未配置：请在后端 .env 设置 ADMIN_TOKEN（或向 system_setting 表插入 key=admin_token），并重启/生效后重试",
        )
    if not x_admin_token or not hmac.compare_digest(str(x_admin_token), effective):
        raise HTTPException(401, "admin token 无效")


# ———————————————————— 运行时配置 ————————————————————

@router.get("/config", dependencies=[Depends(require_admin)])
def get_config(db: Session = Depends(get_db)):
    """生效配置（含 raw 供编辑回填；敏感值同时给脱敏展示）。此接口需 token。"""
    return config_service.masked_view(db)


@router.put("/config", dependencies=[Depends(require_admin)])
def put_config(data: dict[str, str], request: Request, db: Session = Depends(get_db)):
    """更新运行时配置。值为空串 = 清除该覆盖，回落 .env 默认值。立即生效。"""
    try:
        updated = config_service.set_overrides(db, data)
    except KeyError as exc:
        raise HTTPException(400, f"unknown config key: {exc}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit_service.record(
        db, "admin.config_updated", request=request, user=current_user_of(request),
        detail={"keys": updated},
    )
    return {"updated": updated, **config_service.masked_view(db)}


# ———————————————————— 连通性测试 ————————————————————

def _normalize_base(base_url: str) -> str:
    return (base_url or "").rstrip("/")


@router.post("/test/llm", dependencies=[Depends(require_admin)])
def test_llm(payload: dict | None = None, request: Request = None, db: Session = Depends(get_db)):
    """用一条最小请求验证 LLM 连通性；可传入未保存的 base_url/api_key/model 先测后存。"""
    p = payload or {}
    actor = current_user_of(request)
    try:
        base_url = str(p.get("base_url") or config_service.get_effective(db, "llm_base_url"))
        api_key = str(p.get("api_key") or config_service.get_effective(db, "llm_api_key"))
        model = str(p.get("model") or config_service.get_effective(db, "llm_model"))
    except KeyError as exc:
        raise HTTPException(400, f"unknown config key: {exc}")

    if not (base_url and api_key and model):
        raise HTTPException(400, "请先填写 LLM base_url / api_key / model 三项")

    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(400, str(exc))

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字母：OK"}],
        "max_tokens": 8,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.monotonic()
    try:
        resp = httpx.post(
            f"{_normalize_base(base_url)}/chat/completions",
            json=body, headers=headers, timeout=25,
        )
    except httpx.HTTPError as exc:
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(502, f"无法连接 {base_url}：{exc}")
    latency = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "http_status": resp.status_code})
        raise HTTPException(502, f"HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": "响应格式异常"})
        raise HTTPException(502, f"响应格式异常：{resp.text[:300]}")
    audit_service.record(db, "admin.test_llm", request=request, user=actor,
                         detail={"ok": True, "model": model, "latency_ms": latency})
    return {"ok": True, "latency_ms": latency, "model": model, "reply": (content or "").strip()[:80]}


@router.post("/test/embedding", dependencies=[Depends(require_admin)])
def test_embedding(payload: dict | None = None, request: Request = None, db: Session = Depends(get_db)):
    """验证 Embedding 服务连通性，并回读向量维度（应与 EMBED_DIM 一致）。"""
    p = payload or {}
    actor = current_user_of(request)
    try:
        base_url = str(p.get("base_url") or config_service.get_effective(db, "embedding_base_url"))
        api_key = str(p.get("api_key") or config_service.get_effective(db, "embedding_api_key"))
        model = str(p.get("model") or config_service.get_effective(db, "embedding_model"))
    except KeyError as exc:
        raise HTTPException(400, f"unknown config key: {exc}")

    if not (base_url and api_key and model):
        raise HTTPException(400, "请先填写 Embedding base_url / api_key / model 三项")

    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(400, str(exc))

    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.monotonic()
    try:
        resp = httpx.post(
            f"{_normalize_base(base_url)}/embeddings",
            json={"model": model, "input": "连通性测试"}, headers=headers, timeout=25,
        )
    except httpx.HTTPError as exc:
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(502, f"无法连接 {base_url}：{exc}")
    latency = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "http_status": resp.status_code})
        raise HTTPException(502, f"HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        dim = len(resp.json()["data"][0]["embedding"])
    except Exception:
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": "响应格式异常"})
        raise HTTPException(502, f"响应格式异常：{resp.text[:300]}")
    expected = get_settings().embed_dim
    audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                         detail={"ok": True, "model": model, "dim": dim, "latency_ms": latency})
    return {
        "ok": True,
        "latency_ms": latency,
        "model": model,
        "dim": dim,
        "dim_expected": expected,
        "dim_match": dim == expected,
        "warning": None if dim == expected else f"向量维度 {dim} 与 EMBED_DIM={expected} 不一致，聚类相似度会失真",
    }


# ———————————————————— 运行状态总览 ————————————————————

def _collect_stats(db: Session) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total = db.query(func.count(Article.id)).scalar() or 0
    summarized = db.query(func.count(ArticleSummary.id)).scalar() or 0
    embedded = db.query(func.count(Article.id)).filter(Article.embedding.isnot(None)).scalar() or 0
    events_active = db.query(func.count(HotEvent.id)).filter(HotEvent.status != "已平息").scalar() or 0
    articles_24h = db.query(func.count(Article.id)).filter(Article.publish_time >= since).scalar() or 0
    last_crawl = db.query(func.max(Article.created_at)).scalar()
    return {
        "articles_total": total,
        "articles_summarized": summarized,
        "articles_embedded": embedded,
        "events_active": events_active,
        "articles_24h": articles_24h,
        "ai_coverage": round(summarized / total * 100, 1) if total else 0,
        "last_crawl": last_crawl.isoformat() if last_crawl else None,
    }


@router.get("/status", dependencies=[Depends(require_admin)])
def admin_status(db: Session = Depends(get_db)):
    """运维总览：基础设施连通性 + worker 存活 + 数据水位 + 最近任务。"""
    settings = get_settings()
    s = _collect_stats(db)

    # DB
    db_ok = True
    try:
        db.query(func.count(Article.id)).scalar()
    except Exception:
        db_ok = False

    # Redis
    redis_ok, redis_latency = False, None
    try:
        t0 = time.monotonic()
        client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        redis_ok = bool(client.ping())
        redis_latency = int((time.monotonic() - t0) * 1000)
    except Exception:
        redis_ok = False

    # Celery worker（default 队列 worker + ai 队列 worker 都会响应 ping）
    workers: list[str] = []
    workers_error: str | None = None
    try:
        from app.tasks.celery_app import celery_app
        pong = celery_app.control.inspect(timeout=2).ping()
        if isinstance(pong, dict):
            workers = sorted(pong.keys())
    except Exception as exc:
        workers_error = str(exc)[:200]

    # AI 配置是否就绪
    def _configured(prefix: str) -> bool:
        try:
            return all(
                config_service.get_effective(db, f"{prefix}_{k}") for k in ("base_url", "api_key", "model")
            )
        except Exception:
            return False

    llm_configured = _configured("llm")
    embedding_configured = _configured("embedding")
    # Embedding 模型指纹：检测"切换模型后存量向量未重嵌"的漂移（旧模型向量与新模型向量不可比）
    fingerprint = get_embedding_fingerprint(db)
    try:
        current_embedding = str(config_service.get_effective(db, "embedding_model"))
    except Exception:
        current_embedding = ""
    current_fp = f"{current_embedding}|{settings.embed_dim}"
    embedding_stale = bool(fingerprint and fingerprint != current_fp)

    # 最近一次爬虫运行记录
    last_run = (
        db.query(TaskRun).filter(TaskRun.name == "crawler").order_by(TaskRun.id.desc()).first()
    )
    # 最近一次失败任务：若同任务在失败后已有成功运行，视为已恢复，不再告警
    last_failed = (
        db.query(TaskRun).filter(TaskRun.status == "failed").order_by(TaskRun.id.desc()).first()
    )
    failed_recovered = False
    if last_failed:
        failed_recovered = (
            db.query(TaskRun)
            .filter(
                TaskRun.name == last_failed.name,
                TaskRun.status == "success",
                TaskRun.id > last_failed.id,
            )
            .count()
            > 0
        )

    return {
        "app": settings.app_name,
        "version": settings.version,
        "time": datetime.now(timezone.utc).isoformat(),
        "infra": {
            "db": db_ok,
            "redis": redis_ok,
            "redis_latency_ms": redis_latency,
            "celery_workers": workers,
            "celery_workers_error": workers_error,
        },
        "ai": {
            "llm_configured": llm_configured,
            "embedding_configured": embedding_configured,
            "embedding_fingerprint": fingerprint,
            "embedding_stale": embedding_stale,
        },
        "crawler": {
            "last_run": {
                "status": last_run.status,
                "trigger": last_run.trigger,
                "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
                "duration_ms": last_run.duration_ms,
                "detail": last_run.detail,
            }
            if last_run
            else None,
            "interval_minutes": config_service.get_effective(db, "crawler_interval_minutes"),
        },
        "stats": s,
        "last_failed_task": {
            "name": last_failed.name,
            "status": last_failed.status,
            "started_at": last_failed.started_at.isoformat() if last_failed.started_at else None,
            "detail": last_failed.detail,
        }
        if last_failed and not failed_recovered
        else None,
    }


@router.get("/task-runs", dependencies=[Depends(require_admin)])
def task_runs(limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    rows = db.query(TaskRun).order_by(TaskRun.id.desc()).limit(limit).all()
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "trigger": r.trigger,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "duration_ms": r.duration_ms,
                "detail": r.detail,
            }
            for r in rows
        ]
    }


# ———————————————————— Token 用量与成本 ————————————————————

@router.get("/usage/summary", dependencies=[Depends(require_admin)])
def usage_summary_api(
    period: str = Query("today", pattern="^(today|7d|30d|month)$"),
    db: Session = Depends(get_db),
):
    """AI 用量/成本汇总。today 按 UTC 0 点起算，与日报日期口径一致。"""
    return usage_service.usage_summary(db, period)


@router.get("/usage/logs", dependencies=[Depends(require_admin)])
def usage_logs_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """AI 调用流水分页明细（联 article 取标题，error 截断为摘要）。"""
    return usage_service.usage_logs(db, page, page_size)


def _usage_csv_stream(db: Session):
    """分批产出 CSV 行（UTF-8 BOM 由首段携带；首行为对账口径注释）。"""
    yield "\ufeff"  # UTF-8 BOM，方便 Excel 直接打开
    yield f"# {usage_service.CSV_NOTE}\n"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(usage_service.CSV_HEADERS)
    yield buf.getvalue()

    batch = 1000
    last_id = 0
    while True:
        rows = (
            db.query(AiUsageLog)
            .filter(AiUsageLog.id > last_id)
            .order_by(AiUsageLog.id)
            .limit(batch)
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].id  # id 游标：深翻页不因 OFFSET 退化为 O(N²)
        titles = {
            a.id: a.title
            for a in db.query(Article)
            .filter(Article.id.in_([r.article_id for r in rows if r.article_id])).all()
        }
        buf = io.StringIO()
        writer = csv.writer(buf)
        for r in rows:
            writer.writerow([
                r.id,
                r.created_at.isoformat() if r.created_at else "",
                r.task_type,
                r.trigger,
                r.model,
                r.status,
                r.article_id if r.article_id is not None else "",
                titles.get(r.article_id, ""),
                r.event_id if r.event_id is not None else "",
                r.prompt_tokens, r.completion_tokens, r.cached_tokens, r.total_tokens,
                r.latency_ms,
                float(r.cost or 0),
                (r.error or "").replace("\n", " ")[:500],
            ])
        yield buf.getvalue()
        if len(rows) < batch:
            break


@router.get("/usage/export", dependencies=[Depends(require_admin)])
def usage_export(db: Session = Depends(get_db)):
    """全量流水导出 CSV（UTF-8 BOM；首行注释说明本地计费与供应商账单的口径差异）。"""
    return StreamingResponse(
        _usage_csv_stream(db),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ai-usage.csv"},
    )


@router.get("/usage/prices", dependencies=[Depends(require_admin)])
def usage_prices_get(db: Session = Depends(get_db)):
    """模型价目表（每百万 token 单价，人民币元）。"""
    return {"items": [usage_service.price_dict(p) for p in usage_service.list_prices(db)]}


@router.post("/usage/prices", dependencies=[Depends(require_admin)])
def usage_prices_post(data: dict, db: Session = Depends(get_db)):
    """新增 / 更新价目：{model, price_input, price_output}（每百万 token，元）。调价只影响之后的新记录。"""
    model = str(data.get("model") or "").strip()
    if not model or len(model) > 64:
        raise HTTPException(400, "model 必填且长度 ≤ 64")
    try:
        price_input = round(float(data.get("price_input", 0)), 4)
        price_output = round(float(data.get("price_output", 0)), 4)
    except (TypeError, ValueError):
        raise HTTPException(400, "price_input / price_output 必须为数字")
    if price_input < 0 or price_output < 0:
        raise HTTPException(400, "单价不能为负数")
    row = usage_service.upsert_price(db, model, price_input, price_output)
    return {"ok": True, "item": usage_service.price_dict(row)}
