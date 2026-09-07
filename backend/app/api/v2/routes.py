"""API v2（PRD 第 12 节及 V1 能力）。

前端契约说明：所有响应字段与 frontend/src/types.ts 对应，
article 内嵌 summary 与 events，前端可直接渲染。

性能约定：热度打分/排序用 hot_score_expr 在 DB 内完成（_max_raw、hot_board、
recompute_event_scores 共用同一表达式）；列表查询统一 selectinload 预取
summary、批量取事件成员关联，禁止逐行懒加载。
"""
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.algorithms.hot_score import hot_score, hot_score_expr
from app.api.v2.admin import require_admin
from app.core.db import get_db
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.models.summary import ArticleSummary

router = APIRouter(prefix="/api/v2")


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
    raw = hot_score(views, comments, shares, a.publish_time)
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
        (hot_score(a.views or 0, a.comments or 0, a.shares or 0, a.publish_time) for a in articles),
        default=0.0,
    )


def _event_links(db: Session, article_ids: list[int]) -> dict[int, list[int]]:
    if not article_ids:
        return {}
    links = db.query(EventArticle).filter(EventArticle.article_id.in_(article_ids)).all()
    out: dict[int, list[int]] = {}
    for l in links:
        out.setdefault(l.article_id, []).append(l.event_id)
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
    art_ids = sorted({l.article_id for l in links})
    members_by_event: dict[int, list[Article]] = {eid: [] for eid in event_ids}
    if art_ids:
        members_map = {
            a.id: a
            for a in db.query(Article)
            .options(selectinload(Article.summary))
            .filter(Article.id.in_(art_ids))
            .all()
        }
        for l in links:
            a = members_map.get(l.article_id)
            if a is not None:
                members_by_event[l.event_id].append(a)

    max_event_raw = float(db.query(func.max(HotEvent.hot_score)).scalar() or 0.0)
    count_by_event: dict[int, int] = {}
    for l in links:
        count_by_event[l.event_id] = count_by_event.get(l.event_id, 0) + 1
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

@router.get("/health")
def health(db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.query(func.count(Article.id)).scalar()
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "time": datetime.now(timezone.utc).isoformat()}


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

@router.get("/hot/board")
def hot_board(
    channel: str | None = None,
    limit: int = Query(40, le=100),
    db: Session = Depends(get_db),
):
    filters = [Article.publish_time >= datetime.now(timezone.utc) - timedelta(hours=120)]
    if channel and channel != "全部":
        filters.append(Article.channel == channel)

    expr = hot_score_expr(Article)
    total = db.query(func.count(Article.id)).filter(*filters).scalar() or 0
    max_raw = float(db.query(func.max(expr)).filter(*filters).scalar() or 0.0)
    articles = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .filter(*filters)
        .order_by(expr.desc())
        .limit(limit)
        .all()
    )
    if not articles:
        return {"items": [], "total": 0}
    if max_raw <= 0:
        max_raw = _page_max_raw(articles)
    links = _event_links(db, [a.id for a in articles])
    return {
        "items": [_article_dict(a, links, max_raw) for a in articles],
        "total": total,
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
        query = query.filter(Article.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    total = query.count()
    articles = query.order_by(Article.publish_time.desc()).offset(offset).limit(limit).all()
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
def events_list(limit: int = Query(30, le=100), db: Session = Depends(get_db)):
    events = db.query(HotEvent).order_by(HotEvent.hot_score.desc()).limit(limit).all()
    base_raw = _max_raw(db)
    return {
        "items": _events_payload(db, events, base_raw, with_articles=True),
        "total": len(events),
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
    row = db.query(DailyReport).order_by(DailyReport.date.desc()).first()
    if not row:
        raise HTTPException(404, "no report yet")
    return {"date": row.date.isoformat(), "issue_no": row.issue_no, **row.content}


@router.get("/report/dates")
def report_dates(db: Session = Depends(get_db)):
    rows = db.query(DailyReport).order_by(DailyReport.date.desc()).all()
    return {"items": [{"date": r.date.isoformat(), "issue_no": r.issue_no} for r in rows]}


# ———————————————————— 运维触发 ————————————————————

@router.post("/admin/crawl")
def trigger_crawl(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    from app.tasks.tasks import crawler_task

    task = crawler_task.delay(trigger="manual")
    return {"queued": True, "task_id": task.id}


@router.post("/admin/events")
def trigger_events(db: Session = Depends(get_db), _: None = Depends(require_admin)):
    from app.tasks.tasks import event_task

    task = event_task.delay(trigger="manual")
    return {"queued": True, "task_id": task.id}


@router.post("/admin/report")
def trigger_report(
    date: str | None = None,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    from app.tasks.tasks import report_task

    task = report_task.delay(date, trigger="manual")
    return {"queued": True, "task_id": task.id}
