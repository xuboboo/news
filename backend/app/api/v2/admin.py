"""管理面板 API（运维监控 + 运行时配置）。

鉴权双通道（require_admin）：
① super_admin 登录会话（Cookie，前端常规路径）；
② X-Admin-Token 应急后门——与生效 admin_token 一致即放行，供脚本/回归使用；
ADMIN_NO_AUTH=true 时直接放行（仅限内网，公网必须关闭）。
"""
import csv
import hmac
import io
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.ai import llm_client
from app.auth.deps import attach_token_principal, authenticate_session, current_user_of, require_json_mutation
from app.core.config import get_settings
from app.core.db import get_db
from app.models.article import Article
from app.models.event import HotEvent
from app.models.summary import ArticleSummary
from app.models.task_run import TaskRun
from app.models.usage import AiUsageLog, ModelPrice
from app.models.user import ROLE_SUPER_ADMIN
from app.services import audit_service, config_service, usage_service
from app.services.embedding_service import get_embedding_fingerprint
from app.services.infra_status import cadence_warnings, celery_workers_ping, redis_ping
from app.utils.tz import TZ_BJ
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

# 变更类接口统一过 CSRF 防线（X-Requested-With + JSON body）：
# 此前防线只挂在 auth/users 路由，admin 的 PUT/POST（config/test/prices）不在防线内。
router = APIRouter(prefix="/api/v2/admin", dependencies=[Depends(require_json_mutation)])


# ———————————————————— 鉴权 ————————————————————

def require_admin(
    request: Request,
    db: Session = Depends(get_db),
    x_admin_token: str | None = Header(default=None),
):
    if get_settings().admin_no_auth:
        attach_token_principal(request)  # 审计主体可追溯：免鉴权通道记为 admin-token
        return  # 内网免鉴权模式（ADMIN_NO_AUTH=true）
    # ① 登录会话（super_admin）——复用全站唯一会话校验（含空闲超时/账号/租户状态），
    #    此前这里的内联实现与之漂移：不查空闲超时、不校验租户状态
    try:
        user, row = authenticate_session(request, db)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise  # 403（租户停用等）如实上抛；401 落到 token 通道
    else:
        if user.role != ROLE_SUPER_ADMIN:
            raise HTTPException(403, "需要平台超管权限")
        if getattr(user, "must_change_password", False):
            # 与 require_user 的首登门禁对齐：管理面不留绕行口
            raise HTTPException(403, "首次登录请先修改密码")
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
    if not x_admin_token or not hmac.compare_digest(
        str(x_admin_token).encode(), str(effective).encode()
    ):
        # 携带了错误 token 是在试探应急通道，值得留痕（去重防刷表）；
        # 未携带 token 的裸 401 多为扫描噪声，不记
        if x_admin_token:
            audit_service.record_access_denied(
                db, request=request, path=request.url.path, method=request.method,
                reason="invalid_admin_token",
            )
        raise HTTPException(401, "admin token 无效")
    # 审计主体可追溯（2026-09-09 审计修复）：原先 token 通道下 current_user_of
    # 为 None，config_updated 等最敏感操作的审计主体为空、无法追责
    attach_token_principal(request)


# ———————————————————— 运行时配置 ————————————————————

@router.get("/config", dependencies=[Depends(require_admin)])
def get_config(db: Session = Depends(get_db)):
    """生效配置（含 raw 供编辑回填；敏感值同时给脱敏展示）。此接口需 token。"""
    return config_service.masked_view(db)


@router.put("/config", dependencies=[Depends(require_admin)])
def put_config(data: dict[str, str], request: Request, db: Session = Depends(get_db)):
    """更新运行时配置。值为空串 = 清除该覆盖，回落 .env 默认值。立即生效。"""
    old_view = {
        k: config_service.get_effective(db, k)
        for k in (data or {})
        if k in config_service.ALLOWED_KEYS
    }
    try:
        updated = config_service.set_overrides(db, data)
    except KeyError as exc:
        raise HTTPException(400, f"unknown config key: {exc}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # 审计记 old→new：保留期等非敏感值的改动可追责；敏感值只记「已变更」不落内容
    changes = {}
    for k in updated:
        if k in config_service.SENSITIVE_KEYS:
            changes[k] = "（敏感值，不记录内容）"
        else:
            changes[k] = f"{old_view.get(k)} → {config_service.get_effective(db, k)}"
    audit_service.record(
        db, "admin.config_updated", request=request, user=current_user_of(request),
        detail={"keys": updated, "changes": changes},
    )
    return {"updated": updated, **config_service.masked_view(db)}


# ———————————————————— 连通性测试 ————————————————————

def _normalize_base(base_url: str) -> str:
    return (base_url or "").rstrip("/")


def _safe_json(resp) -> dict:
    """响应体转 dict；非 JSON（HTML 错误页等）返回空 dict。"""
    try:
        return resp.json()
    except Exception:
        return {}


def _log_conn_usage(model: str, status: str, latency_ms: int,
                    error: str | None = None, body: dict | None = None,
                    usage: dict | None = None) -> None:
    """「测试连接」的用量落账：真实供应商调用烧 token，必须与业务调用同一本账，
    否则看板成本低估（ai_usage_log 完整性）。失败早于 usage 提取时 body 缺省=全 0；
    双协议归一化用量可经 usage 直传（anthropic 响应体 extract_openai_usage 读不出）。"""
    usage_service.record_usage(
        task_type="conn_test", trigger="manual", model=model, status=status,
        latency_ms=latency_ms, error=error,
        **(usage if usage is not None else usage_service.extract_openai_usage(body)),
    )


@router.post("/test/llm", dependencies=[Depends(require_admin)])
def test_llm(payload: dict | None = None, request: Request = None, db: Session = Depends(get_db)):
    """用一条最小请求验证 LLM 连通性；可传入未保存的 base_url/api_key/model/protocol 先测后存。

    protocol 省略时取生效配置；双协议请求构造与响应解析复用 llm_client，
    anthropic 走 /v1/messages + x-api-key（usage 由适配层归一化后再落账）。
    """
    p = payload or {}
    actor = current_user_of(request)
    try:
        base_url = str(p.get("base_url") or config_service.get_effective(db, "llm_base_url"))
        api_key = str(p.get("api_key") or config_service.get_effective(db, "llm_api_key"))
        model = str(p.get("model") or config_service.get_effective(db, "llm_model"))
        protocol = str(p.get("protocol") or config_service.get_effective(db, "llm_protocol") or "openai")
    except KeyError as exc:
        raise HTTPException(400, f"unknown config key: {exc}")
    try:
        protocol = llm_client.normalize_protocol(protocol)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not (base_url and api_key and model):
        raise HTTPException(400, "请先填写 LLM base_url / api_key / model 三项")

    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(400, str(exc))

    url, headers, body = llm_client.build_request(
        base_url, api_key, model, protocol, "只回复两个字母：OK", max_tokens=8,
    )
    start = time.monotonic()
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=25)
    except httpx.HTTPError as exc:
        _log_conn_usage(model, "api_fail", int((time.monotonic() - start) * 1000), error=str(exc))
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(502, f"无法连接 {base_url}：{exc}")
    latency = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        _log_conn_usage(model, "api_fail", latency, error=f"HTTP {resp.status_code}",
                        body=_safe_json(resp))
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "http_status": resp.status_code})
        raise HTTPException(502, f"HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        content, usage_map = llm_client.parse_response(protocol, resp)
    except llm_client.LLMFormatError as exc:
        _log_conn_usage(model, "parse_fail", latency, error="响应格式异常", usage=exc.usage)
        audit_service.record(db, "admin.test_llm", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": "响应格式异常"})
        raise HTTPException(502, f"响应格式异常：{resp.text[:300]}")
    _log_conn_usage(model, "success", latency, usage=usage_map)
    audit_service.record(db, "admin.test_llm", request=request, user=actor,
                         detail={"ok": True, "model": model, "protocol": protocol, "latency_ms": latency})
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
        _log_conn_usage(model, "api_fail", int((time.monotonic() - start) * 1000), error=str(exc))
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": str(exc)[:200]})
        raise HTTPException(502, f"无法连接 {base_url}：{exc}")
    latency = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        _log_conn_usage(model, "api_fail", latency, error=f"HTTP {resp.status_code}",
                        body=_safe_json(resp))
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "http_status": resp.status_code})
        raise HTTPException(502, f"HTTP {resp.status_code}：{resp.text[:300]}")
    try:
        dim = len(resp.json()["data"][0]["embedding"])
    except Exception:
        _log_conn_usage(model, "parse_fail", latency, error="响应格式异常", body=_safe_json(resp))
        audit_service.record(db, "admin.test_embedding", request=request, user=actor,
                             detail={"ok": False, "model": model, "error": "响应格式异常"})
        raise HTTPException(502, f"响应格式异常：{resp.text[:300]}")
    _log_conn_usage(model, "success", latency, body=_safe_json(resp))
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

def _collect_stats(db: Session, crawler_last_started: datetime | None = None) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total = db.query(func.count(Article.id)).scalar() or 0
    summarized = db.query(func.count(ArticleSummary.id)).scalar() or 0
    embedded = db.query(func.count(Article.id)).filter(Article.embedding.isnot(None)).scalar() or 0
    events_active = db.query(func.count(HotEvent.id)).filter(HotEvent.status != "已平息").scalar() or 0
    articles_24h = db.query(func.count(Article.id)).filter(Article.publish_time >= since).scalar() or 0
    last_ingest = db.query(func.max(Article.created_at)).scalar()
    return {
        "articles_total": total,
        "articles_summarized": summarized,
        "articles_embedded": embedded,
        "events_active": events_active,
        "articles_24h": articles_24h,
        "ai_coverage": round(summarized / total * 100, 1) if total else 0,
        # 「最近采集」以 TaskRun(crawler) 最近一轮为准：用 max(created_at) 当采集时间
        # 会在来源断供/连续 0 新文章时越来越旧，误报「采集停了」；入库时间另列 last_ingest
        "last_crawl": crawler_last_started.isoformat() if crawler_last_started else None,
        "last_ingest": last_ingest.isoformat() if last_ingest else None,
    }


# /status 结果短缓存：探测（Redis/Celery 广播 ping）+ 多个全表 COUNT 偏重，
# 多管理员/多标签页每 30s 轮询会线性放大；10s 缓存对运维面板足够新鲜
_STATUS_CACHE: dict = {"t": 0.0, "data": None}
_STATUS_LOCK = threading.Lock()
_STATUS_TTL_SECONDS = 10.0


@router.get("/status", dependencies=[Depends(require_admin)])
def admin_status(
    fresh: bool = Query(False, description="跳过缓存强制重算"),
    db: Session = Depends(get_db),
):
    """运维总览：基础设施连通性 + worker 存活 + 数据水位 + 任务节奏心跳 + 最近任务。"""
    if not fresh:
        with _STATUS_LOCK:
            cached = _STATUS_CACHE["data"]
            if cached is not None and time.monotonic() - _STATUS_CACHE["t"] < _STATUS_TTL_SECONDS:
                return cached
    data = _compute_status(db)
    with _STATUS_LOCK:
        _STATUS_CACHE["t"] = time.monotonic()
        _STATUS_CACHE["data"] = data
    return data


def _compute_status(db: Session) -> dict:
    settings = get_settings()

    # 最近一次爬虫运行记录（先取：「最近采集」口径与下方 crawler 字段共用）
    last_run = (
        db.query(TaskRun).filter(TaskRun.name == "crawler").order_by(TaskRun.id.desc()).first()
    )
    s = _collect_stats(db, last_run.started_at if last_run else None)

    # DB
    db_ok = True
    try:
        db.query(func.count(Article.id)).scalar()
    except Exception:
        db_ok = False

    # Redis（模块级共享连接池，复用连接不再每次新建 client）
    redis_ok, redis_latency = redis_ping()

    # Celery worker（default 队列 worker + ai 队列 worker 都会响应 ping；4s 超时 + 空结果重试一次）
    workers, workers_error = celery_workers_ping()

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

    # 互动数据轮询水位（crawler/stats.py）：到期/待回填/已放弃/近24h成功轮询。
    # 范围与 poll_due_articles 同口径（stats_source_filter）：IT之家 + 快科技——
    # 范围外来源不排期轮询，漏加会让「待回填」永远清不掉
    from app.crawler.stats import GIVE_UP_FAILS, stats_source_filter

    now_utc = datetime.now(timezone.utc)
    stats_since = now_utc - timedelta(hours=24)
    try:
        stats_enabled = config_service.get_effective(db, "stats_poll_enabled")
    except Exception:
        stats_enabled = None

    # 任务节奏心跳：beat 是单点且 inspect.ping() 看不见它——beat 挂掉后 worker 仍在线、
    # 面板全绿，但定时任务（聚类/轮询/日报）全部停摆。用「最近一次运行是否超出预期节奏」
    # 感知调度停摆；stats 被面板热停（enabled=false）时不产生记录，不参与判停。
    interval_minutes = config_service.get_effective(db, "crawler_interval_minutes")
    heartbeat = cadence_warnings(
        db, interval_minutes, skip={"stats"} if stats_enabled is False else set()
    )
    due_now = (
        db.query(func.count(Article.id))
        .filter(stats_source_filter(), Article.stats_next_poll_at.isnot(None), Article.stats_next_poll_at <= now_utc)
        .scalar() or 0
    )
    unscheduled = and_(
        Article.stats_polled_at.is_(None), Article.stats_next_poll_at.is_(None)
    )
    pending_backfill = (
        db.query(func.count(Article.id))
        .filter(stats_source_filter(), unscheduled, Article.stats_fail_count < GIVE_UP_FAILS)
        .scalar() or 0
    )
    gave_up = (
        db.query(func.count(Article.id))
        .filter(stats_source_filter(), unscheduled, Article.stats_fail_count >= GIVE_UP_FAILS)
        .scalar() or 0
    )
    polled_24h = (
        db.query(func.count(Article.id))
        .filter(stats_source_filter(), Article.stats_polled_at.isnot(None), Article.stats_polled_at >= stats_since)
        .scalar() or 0
    )
    last_stats_run = (
        db.query(TaskRun).filter(TaskRun.name == "stats").order_by(TaskRun.id.desc()).first()
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
            "interval_minutes": interval_minutes,
        },
        "stats_poll": {
            "enabled": stats_enabled,
            "due_now": due_now,
            "pending_backfill": pending_backfill,
            "gave_up": gave_up,
            "polled_24h": polled_24h,
            "last_run": {
                "status": last_stats_run.status,
                "trigger": last_stats_run.trigger,
                "started_at": last_stats_run.started_at.isoformat() if last_stats_run.started_at else None,
                "duration_ms": last_stats_run.duration_ms,
                "detail": last_stats_run.detail,
            }
            if last_stats_run
            else None,
        },
        "heartbeat": heartbeat,
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
def task_runs(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
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
    daily: bool = Query(True, description="是否计算近 30 天按日趋势（仅 30d 视图需要）"),
    groups: bool = Query(True, description="是否计算按任务/模型分组（仅 30d 视图需要）"),
    db: Session = Depends(get_db),
):
    """AI 用量/成本汇总。today/month/日趋势均按北京时间自然日切分。"""
    payload = usage_service.usage_summary(db, period, include_daily=daily, include_groups=groups)
    # 当日生效预算随汇总返回：前端今日成本卡直接展示预算用量（0 = 未启用）
    try:
        payload["daily_budget"] = float(config_service.get_effective(db, "ai_daily_cost_budget") or 0)
    except Exception:
        payload["daily_budget"] = 0.0
    return payload


@router.get("/usage/logs", dependencies=[Depends(require_admin)])
def usage_logs_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    # pattern 从 usage_service 常量拼装（单一事实来源；conn_test 也允许过滤）
    task_type: str | None = Query(
        None, pattern="^(" + "|".join(usage_service.LOG_TASK_TYPES) + ")$"
    ),
    status: str | None = Query(
        None, pattern="^(" + "|".join(usage_service.LOG_STATUSES) + ")$"
    ),
    model: str | None = Query(None, max_length=64, description="按模型名精确过滤"),
    db: Session = Depends(get_db),
):
    """AI 调用流水分页明细（联 article 取标题，error 截断为摘要；可按任务/状态/模型过滤）。"""
    return usage_service.usage_logs(db, page, page_size, task_type=task_type, status=status, model=model)


def _usage_csv_stream(
    db: Session,
    start: datetime | None = None,
    end: datetime | None = None,
    task_type: str | None = None,
    status: str | None = None,
):
    """分批产出 CSV 行（UTF-8 BOM 由首段携带；首行为对账口径注释）。

    start/end：可选北京时间自然日范围（左闭右开）；task_type/status：可选流水过滤
    （与 /usage/logs 同一白名单）。外部文本字段过 csv_safe_cell 防公式注入；
    cost 固定 6 位小数，避免极小值变科学计数法。
    """
    yield "\ufeff"  # UTF-8 BOM，方便 Excel 直接打开
    yield f"# {usage_service.CSV_NOTE}\n"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(usage_service.CSV_HEADERS)
    yield buf.getvalue()

    batch = 1000
    last_id = 0
    while True:
        q = db.query(AiUsageLog).filter(AiUsageLog.id > last_id)
        if start is not None:
            q = q.filter(AiUsageLog.created_at >= start)
        if end is not None:
            q = q.filter(AiUsageLog.created_at < end)
        if task_type:
            q = q.filter(AiUsageLog.task_type == task_type)
        if status:
            q = q.filter(AiUsageLog.status == status)
        rows = q.order_by(AiUsageLog.id).limit(batch).all()
        if not rows:
            break
        last_id = rows[-1].id  # id 游标：深翻页不因 OFFSET 退化为 O(N²)
        art_ids = [r.article_id for r in rows if r.article_id]
        # 空批次不查 article（此前无条件 IN 查询，本批无文章引用时也白打一条 SQL）
        titles = (
            {
                a.id: a.title
                for a in db.query(Article).filter(Article.id.in_(art_ids)).all()
            }
            if art_ids
            else {}
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        for r in rows:
            writer.writerow([
                r.id,
                r.created_at.isoformat() if r.created_at else "",
                usage_service.csv_safe_cell(r.task_type),
                usage_service.csv_safe_cell(r.trigger),
                usage_service.csv_safe_cell(r.model),
                usage_service.csv_safe_cell(r.status),
                r.article_id if r.article_id is not None else "",
                usage_service.csv_safe_cell(titles.get(r.article_id, "")),
                r.event_id if r.event_id is not None else "",
                r.prompt_tokens, r.completion_tokens, r.cached_tokens, r.total_tokens,
                r.latency_ms,
                f"{float(r.cost or 0):.6f}",
                usage_service.csv_safe_cell((r.error or "").replace("\n", " ")[:500]),
            ])
        yield buf.getvalue()
        if len(rows) < batch:
            break


@router.get("/usage/export", dependencies=[Depends(require_admin)])
def usage_export(
    start: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    task_type: str | None = Query(
        None, pattern="^(" + "|".join(usage_service.LOG_TASK_TYPES) + ")$"
    ),
    status: str | None = Query(
        None, pattern="^(" + "|".join(usage_service.LOG_STATUSES) + ")$"
    ),
    db: Session = Depends(get_db),
):
    """流水导出 CSV（UTF-8 BOM；首行注释说明本地计费与供应商账单的口径差异）。

    start/end 可选（含端点、按北京时间日界），task_type/status 可选（与明细页过滤
    同白名单，前端把当前筛选一并透传），不传 = 全量导出。
    """
    start_dt = datetime.fromisoformat(start).replace(tzinfo=TZ_BJ) if start else None
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=TZ_BJ) + timedelta(days=1) if end else None
    )
    filename = "ai-usage"
    scope = []
    if start or end:
        scope.append(f"{start or 'begin'}-{end or 'now'}")
    if task_type:
        scope.append(task_type)
    if status:
        scope.append(status)
    if scope:
        filename += "-" + "-".join(scope)
    return StreamingResponse(
        _usage_csv_stream(db, start_dt, end_dt, task_type=task_type, status=status),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
    )


@router.get("/usage/prices", dependencies=[Depends(require_admin)])
def usage_prices_get(db: Session = Depends(get_db)):
    """模型价目表（每百万 token 单价，人民币元；price_cache=null 表示按输入价全价计）。"""
    return {"items": [usage_service.price_dict(p) for p in usage_service.list_prices(db)]}


@router.post("/usage/prices", dependencies=[Depends(require_admin)])
def usage_prices_post(data: dict, request: Request, db: Session = Depends(get_db)):
    """新增 / 更新价目：{model, price_input, price_output[, price_cache]}（每百万 token，元）。

    price_cache 传 null / 缺省 = 清除缓存价（回落输入价全价口径）。
    调价只影响之后的新记录；变更写审计（admin.price_updated，含新旧值）。
    """
    model = str(data.get("model") or "").strip()
    if not model or len(model) > 64:
        raise HTTPException(400, "model 必填且长度 ≤ 64")
    try:
        price_input = round(float(data.get("price_input", 0)), 4)
        price_output = round(float(data.get("price_output", 0)), 4)
        raw_cache = data.get("price_cache")
        price_cache = None if raw_cache in (None, "") else round(float(raw_cache), 4)
    except (TypeError, ValueError):
        raise HTTPException(400, "price_input / price_output / price_cache 必须为数字")
    if price_input < 0 or price_output < 0 or (price_cache is not None and price_cache < 0):
        raise HTTPException(400, "单价不能为负数")

    old = db.get(ModelPrice, model)
    try:
        row, created = usage_service.upsert_price(db, model, price_input, price_output, price_cache)
    except ValueError as exc:
        # 服务层防呆（非有限值/负数/超上限）→ 400，不产生半截审计
        raise HTTPException(400, str(exc))
    audit_service.record(
        db, "admin.price_updated", request=request, user=current_user_of(request),
        detail={
            "model": model,
            "op": "create" if created else "update",
            "old": None if old is None else usage_service.price_dict(old),
            "new": usage_service.price_dict(row),
        },
    )
    return {"ok": True, "created": created, "item": usage_service.price_dict(row)}


@router.delete("/usage/prices/{model}", dependencies=[Depends(require_admin)])
def usage_prices_delete(model: str, request: Request, db: Session = Depends(get_db)):
    """删除某模型价目（误录入的模型名得以移除；404 = 价目不存在）。

    历史流水按入库时锁定的单价计，删除只影响之后的新调用（无价记 token、cost=0，
    盲区告警会重新提示——自愈可见）。删除写审计（admin.price_deleted，含被删价目）。
    """
    old = db.get(ModelPrice, model)
    if old is None:
        raise HTTPException(404, f"价目不存在：{model}")
    if not usage_service.delete_price(db, model):
        raise HTTPException(404, f"价目不存在：{model}")
    audit_service.record(
        db, "admin.price_deleted", request=request, user=current_user_of(request),
        detail={"model": model, "deleted": usage_service.price_dict(old)},
    )
    return {"ok": True, "model": model}
