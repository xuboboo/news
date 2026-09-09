"""API v2（PRD 第 12 节及 V1 能力）。

前端契约说明：所有响应字段与 frontend/src/types.ts 对应，
article 内嵌 summary 与 events，前端可直接渲染。

性能约定：热度打分/排序用 hot_score_expr 在 DB 内完成（_max_raw、hot_board、
recompute_event_scores 共用同一表达式）；列表查询统一 selectinload 预取
summary、批量取事件成员关联，禁止逐行懒加载。
"""
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.algorithms.hot_score import hot_score, hot_score_expr
from app.api.v2.admin import require_admin
from app.auth.deps import require_json_mutation
from app.core.db import get_db
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.models.summary import ArticleSummary

# 变更类接口（/admin/crawl|events|stats|report 触发等 POST）统一过 CSRF 防线；
# GET/HEAD/OPTIONS 在守卫内部直接放行，业务读接口不受影响
router = APIRouter(prefix="/api/v2", dependencies=[Depends(require_json_mutation)])


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _escape_like(term: str) -> str:
    """ilike 通配符转义：用户输入中的 % _ \\ 不改变匹配语义。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _summary_dict(s: ArticleSummary | None) -> dict | None:
    if not s:
        return None
    return {
        "summary_short": s.summary_short,
        "summary_long": s.summary_long,
        "key_points": s.key_points or [],
        "tags": s.tags or [],
        "model": s.model,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _article_dict(a: Article, event_ids: dict[int, list[int]] | None = None, max_raw: float = 0) -> dict:
    views, comments, shares = a.views or 0, a.comments or 0, a.shares or 0
    likes = a.likes or 0
    raw = hot_score(views, comments, shares, likes, a.publish_time)
    hs = round((raw / max_raw) * 100, 1) if max_raw > 0 else round(raw, 1)
    return {
        "id": a.id,
        "title": a.title,
        "url": a.url,
        "channel": a.channel,
        "source": a.source,
        "publish_time": _tz(a.publish_time).isoformat(),
        "views": views,
        "comments": comments,
        "shares": shares,
        "likes": likes,
        "hot_score": hs,
        "ai_status": a.ai_status,
        "summary": _summary_dict(a.summary if hasattr(a, "summary") else None),
        "event_ids": (event_ids or {}).get(a.id, []),
    }


def _max_raw(db: Session) -> float:
    """近期最大原始热度（归一化基准），DB 内聚合。"""
    since = datetime.now(timezone.utc) - timedelta(hours=120)
    val = (
        db.query(func.max(hot_score_expr(Article)))
        .filter(Article.publish_time >= since)
        .scalar()
    )
    return float(val or 0.0)


def _page_max_raw(articles: list[Article]) -> float:
    """max_raw 兜底：近 120h 无数据时用当页最大原始分，维持 0-100 归一化语义。"""
    return max(
        (hot_score(a.views or 0, a.comments or 0, a.shares or 0, a.likes or 0, a.publish_time) for a in articles),
        default=0.0,
    )


def _event_links(db: Session, article_ids: list[int]) -> dict[int, list[int]]:
    if not article_ids:
        return {}
    links = db.query(EventArticle).filter(EventArticle.article_id.in_(article_ids)).all()
    out: dict[int, list[int]] = {}
    for lnk in links:
        out.setdefault(lnk.article_id, []).append(lnk.event_id)
    return out


def _events_payload(db: Session, events: list[HotEvent], max_raw: float, with_articles: bool = True) -> list[dict]:
    """批量组装事件响应（列表/详情共用）：一次取全部成员关联与成员文章，
    消灭逐事件 N+1。事件分数按全量事件最大原始分归一化——列表与详情口径一致，
    同一事件两处展示的 hot_score 恒等。"""
    event_ids = [e.id for e in events]
    links = (
        db.query(EventArticle).filter(EventArticle.event_id.in_(event_ids)).all()
        if event_ids
        else []
    )
    art_ids = sorted({lnk.article_id for lnk in links})
    members_by_event: dict[int, list[Article]] = {eid: [] for eid in event_ids}
    if art_ids:
        members_map = {
            a.id: a
            for a in db.query(Article)
            .options(selectinload(Article.summary))
            .filter(Article.id.in_(art_ids))
            .all()
        }
        for lnk in links:
            a = members_map.get(lnk.article_id)
            if a is not None:
                members_by_event[lnk.event_id].append(a)

    max_event_raw = float(db.query(func.max(HotEvent.hot_score)).scalar() or 0.0)
    count_by_event: dict[int, int] = {}
    for lnk in links:
        count_by_event[lnk.event_id] = count_by_event.get(lnk.event_id, 0) + 1
    payload = []
    for ev in events:
        members = sorted(members_by_event[ev.id], key=lambda x: _tz(x.publish_time))
        if max_event_raw > 0:
            hs = round((ev.hot_score / max_event_raw) * 100, 1)
        else:
            hs = round(ev.hot_score, 1)
        data = {
            "id": ev.id,
            "title": ev.title,
            "summary": ev.summary,
            "impacts": ev.impacts or [],
            "status": ev.status,
            "hot_score": hs,
            "first_time": _tz(ev.first_time).isoformat() if ev.first_time else None,
            "last_time": _tz(ev.last_time).isoformat() if ev.last_time else None,
            "timeline": ev.timeline or [],
            "articles_count": count_by_event.get(ev.id, 0),
            "sources_count": len({m.source for m in members}),
        }
        if with_articles:
            data["articles"] = [_article_dict(a, {}, max_raw) for a in members]
        payload.append(data)
    return payload


# ———————————————————— 健康 / 统计 ————————————————————
# /health 已移至 public.py（全站门禁的公开探针例外）；stats 需登录。

@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total = db.query(func.count(Article.id)).scalar() or 0
    summarized = db.query(func.count(ArticleSummary.id)).scalar() or 0
    embedded = db.query(func.count(Article.id)).filter(Article.embedding.isnot(None)).scalar() or 0
    events = db.query(func.count(HotEvent.id)).scalar() or 0
    active_events = db.query(func.count(HotEvent.id)).filter(HotEvent.status != "已平息").scalar() or 0
    last_crawl = db.query(func.max(Article.created_at)).scalar()
    return {
        "articles_total": total,
        "articles_summarized": summarized,
        "articles_embedded": embedded,
        "events_total": events,
        "events_active": active_events,
        "articles_24h": db.query(func.count(Article.id)).filter(Article.publish_time >= since).scalar() or 0,
        "last_crawl": _tz(last_crawl).isoformat() if last_crawl else None,
        "ai_coverage": round(summarized / total * 100, 1) if total else 0,
    }


# ———————————————————— 热榜中心 ————————————————————

# 窗口参数 → 小时数；"all" 取一年（业务上等价全量，避免无界查询）
_HOT_WINDOWS: dict[str, int] = {"24h": 24, "3d": 72, "7d": 168, "all": 8760}


@router.get("/hot/board")
def hot_board(
    channel: str | None = None,
    window: str = Query("24h", pattern="^(24h|3d|7d|all)$"),
    limit: int = Query(40, le=100),
    db: Session = Depends(get_db),
):
    hours = _HOT_WINDOWS[window]
    now = datetime.now(timezone.utc)
    filters = [Article.publish_time >= now - timedelta(hours=hours)]
    if channel and channel != "全部":
        filters.append(Article.channel == channel)

    expr = hot_score_expr(Article)
    total = db.query(func.count(Article.id)).filter(*filters).scalar() or 0
    # 归一化基准取窗口内全频道最大原始分（不含频道过滤）：
    # 切换频道筛选时分数保持稳定可比，不随选择范围缩放
    max_raw = float(
        db.query(func.max(expr))
        .filter(Article.publish_time >= now - timedelta(hours=hours))
        .scalar()
        or 0.0
    )
    articles = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .filter(*filters)
        # 稳定二级排序：同分不再随刷新抖动（2026-09-09 审计）
        .order_by(expr.desc(), Article.publish_time.desc(), Article.id.desc())
        .limit(limit)
        .all()
    )
    if not articles:
        return {"items": [], "total": 0, "window": window}
    if max_raw <= 0:
        max_raw = _page_max_raw(articles)
    links = _event_links(db, [a.id for a in articles])
    return {
        "items": [_article_dict(a, links, max_raw) for a in articles],
        "total": total,
        "window": window,
    }


# ———————————————————— 最新资讯 ————————————————————

@router.get("/articles/latest")
def articles_latest(
    channel: str | None = None,
    q: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Article).options(selectinload(Article.summary))
    if channel and channel != "全部":
        query = query.filter(Article.channel == channel)
    if q:
        # 与前端口径一致：标题或标签任一命中（2026-09-09 审计：旧实现仅搜标题）
        like = f"%{_escape_like(q)}%"
        query = (
            query.outerjoin(ArticleSummary, ArticleSummary.article_id == Article.id)
            .filter(
                or_(
                    Article.title.ilike(like, escape="\\"),
                    cast(ArticleSummary.tags, String).ilike(like, escape="\\"),
                )
            )
        )
    total = query.count()
    # 稳定二级排序：同一 publish_time 的文章顺序不随刷新抖动
    articles = (
        query.order_by(Article.publish_time.desc(), Article.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    max_raw = _max_raw(db)
    if max_raw <= 0:
        max_raw = _page_max_raw(articles)
    links = _event_links(db, [a.id for a in articles])
    return {"items": [_article_dict(a, links, max_raw) for a in articles], "total": total}


@router.get("/article/{article_id}")
def article_detail(article_id: int, db: Session = Depends(get_db)):
    a = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .filter(Article.id == article_id)
        .first()
    )
    if not a:
        raise HTTPException(404, "article not found")
    max_raw = _max_raw(db)
    return _article_dict(a, _event_links(db, [a.id]), max_raw)


# ———————————————————— AI 摘要（PRD 12）————————————————————

@router.get("/article/{article_id}/summary")
def article_summary(article_id: int, db: Session = Depends(get_db)):
    s = db.query(ArticleSummary).filter_by(article_id=article_id).first()
    if not s:
        raise HTTPException(404, "summary not ready")
    return {
        "article_id": article_id,
        "summary": s.summary_short,
        "summary_long": s.summary_long,
        "points": s.key_points or [],
        "tags": s.tags or [],
        "model": s.model,
    }


# ———————————————————— 热点事件（PRD 12）————————————————————

@router.get("/events")
def events_list(
    limit: int = Query(30, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    q: str | None = None,
    min_members: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """事件列表：热度降序 + 过滤/分页。

    total 为库内符合条件的真实总数（旧实现返回 len(当前页)，前端无法据此判断
    是否还有更多事件）；status/q/min_members 供事件页过滤与「只看多来源聚合」
    视图使用；offset 配合 limit 翻页（超过 top-N 的已平息事件不再不可达）。
    """
    query = db.query(HotEvent)
    if status and status != "全部":
        query = query.filter(HotEvent.status == status)
    if q:
        query = query.filter(HotEvent.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if min_members > 0:
        query = query.filter(
            HotEvent.id.in_(
                select(EventArticle.event_id)
                .group_by(EventArticle.event_id)
                .having(func.count(EventArticle.article_id) >= min_members)
            )
        )
    total = query.count()
    events = (
        query.order_by(HotEvent.hot_score.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    base_raw = _max_raw(db)
    return {
        "items": _events_payload(db, events, base_raw, with_articles=True),
        "total": total,
    }


@router.get("/events/{event_id}")
def event_detail(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(HotEvent, event_id)
    if not ev:
        raise HTTPException(404, "event not found")
    return _events_payload(db, [ev], _max_raw(db))[0]


# ———————————————————— 科技日报（PRD 12）————————————————————

@router.get("/report/daily")
def report_daily(date: str | None = None, db: Session = Depends(get_db)):
    if date:
        try:
            day = date_type.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, "date format must be YYYY-MM-DD")
        row = db.query(DailyReport).filter_by(date=day).first()
        if not row:
            raise HTTPException(404, "report not generated for this date")
        return {"date": row.date.isoformat(), "issue_no": row.issue_no, **row.content}
    # latest 取最近一个有实质内容的报告：全空报告（如凌晨窗口刚开启时的误生成占位）
    # 不应顶掉前一日的有效报告——否则日报页白天展示一整天空内容
    rows = db.query(DailyReport).order_by(DailyReport.date.desc()).limit(7).all()
    if not rows:
        raise HTTPException(404, "no report yet")
    row = next(
        (
            r
            for r in rows
            if (r.content or {}).get("top_events") or (r.content or {}).get("quick_news")
        ),
        rows[0],  # 冷启动期连续全空时回退旧行为（返回最新一条）
    )
    return {"date": row.date.isoformat(), "issue_no": row.issue_no, **row.content}


@router.get("/report/dates")
def report_dates(limit: int = Query(60, ge=1, le=366), db: Session = Depends(get_db)):
    """日报期号列表（新→旧）。limit 控制拉取规模，默认最近 60 期。"""
    rows = db.query(DailyReport).order_by(DailyReport.date.desc()).limit(limit).all()
    return {"items": [{"date": r.date.isoformat(), "issue_no": r.issue_no} for r in rows]}


# ———————————————————— 运维触发 ————————————————————

def _audit_trigger(request, db: Session, action: str, task_id: str) -> None:
    from app.auth.deps import current_user_of
    from app.services import audit_service

    audit_service.record(db, action, request=request, user=current_user_of(request),
                         detail={"task_id": task_id})


@router.post("/admin/crawl")
def trigger_crawl(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    from app.tasks.tasks import crawler_task

    task = crawler_task.delay(trigger="manual")
    _audit_trigger(request, db, "admin.trigger_crawl", task.id)
    return {"queued": True, "task_id": task.id}


@router.post("/admin/events")
def trigger_events(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    from app.tasks.tasks import event_task

    task = event_task.delay(trigger="manual")
    _audit_trigger(request, db, "admin.trigger_events", task.id)
    return {"queued": True, "task_id": task.id}


@router.post("/admin/stats")
def trigger_stats_poll(request: Request, db: Session = Depends(get_db), _: None = Depends(require_admin)):
    from app.tasks.tasks import stats_task

    task = stats_task.delay(trigger="manual")
    _audit_trigger(request, db, "admin.trigger_stats", task.id)
    return {"queued": True, "task_id": task.id}


@router.post("/admin/report")
def trigger_report(
    date: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    from app.tasks.tasks import report_task

    task = report_task.delay(date, trigger="manual")
    _audit_trigger(request, db, "admin.trigger_report", task.id)
    return {"queued": True, "task_id": task.id}
