"""日报服务（PRD 第 7 节）：每日自动生成 / report_task 落库。"""
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.algorithms.hot_score import hot_score
from app.core.config import get_settings
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.services import usage_service
from sqlalchemy import text as sql_text


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def issue_no_for(day: date_type) -> int:
    s = get_settings()
    start = datetime.fromisoformat(s.report_issue_epoch).date()
    return max(1, (day - start).days + 1)


def build_report(db: Session, day: date_type) -> dict:
    """构建日报内容（前端可直接渲染的结构）。"""
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    def _in_day(dt: datetime) -> bool:
        dt = _tz(dt)
        return day_start <= dt < day_end

    day_articles = [
        a for a in db.query(Article)
        .filter(Article.publish_time >= day_start - timedelta(hours=2),
                Article.publish_time < day_end + timedelta(hours=2))
        .all()
        if _in_day(a.publish_time)
    ]

    def with_score(a: Article):
        return a, hot_score(a.views, a.comments, a.shares, a.likes or 0, a.publish_time)

    scored = sorted(map(with_score, day_articles), key=lambda p: p[1], reverse=True)
    max_raw = scored[0][1] if scored else 0

    # —— TOP 事件（当天有报道的事件）——
    top_events = []
    events = db.query(HotEvent).all()
    max_ev_raw = max((e.hot_score for e in events), default=0)
    for ev in events:
        links = db.query(EventArticle).filter_by(event_id=ev.id).all()
        member_ids = [lnk.article_id for lnk in links]
        members = [a for a in day_articles if a.id in member_ids]
        if not members:
            continue
        # 口径统一：articles_count 与 sources_count 都按事件全量成员统计
        # （sources_count 原先只数当日成员，与 articles_count 口径不一致）
        all_member_sources = {
            row[0]
            for row in db.query(Article.source)
            .filter(Article.id.in_(member_ids))
            .all()
        }
        top_events.append({
            "id": ev.id,
            "title": ev.title,
            "summary": ev.summary,
            "status": ev.status,
            "hot_score": round((ev.hot_score / max_ev_raw) * 100, 1) if max_ev_raw > 0 else round(ev.hot_score, 1),
            "articles_count": len(member_ids),
            "sources_count": len(all_member_sources),
        })
    top_events = sorted(top_events, key=lambda e: e["hot_score"], reverse=True)[:5]

    # —— 热点速览 / TOP5 ——
    def art_brief(a: Article) -> dict:
        _, hs = with_score(a)
        return {
            "id": a.id,
            "title": a.title,
            "source": a.source,
            "channel": a.channel,
            "hot_score": round(hs, 1),
            "url": a.url,
            "publish_time": _tz(a.publish_time).isoformat(),
            "summary_short": a.summary.summary_short if a.summary else "",
            "tags": (a.summary.tags if a.summary else [])[:4],
        }

    quick_news = [art_brief(a) for a, _ in scored[:8]]
    top_articles = [art_brief(a) for a, _ in scored[:5]]

    # —— 行业变化 ——
    industry_changes = []
    for ev_id in [e["id"] for e in top_events]:
        ev = db.get(HotEvent, ev_id)
        if ev and ev.impacts:
            industry_changes.append(str(ev.impacts[0]))
    if not industry_changes:
        industry_changes = [
            a.summary.key_points[0]
            for a, _ in scored[:4]
            if a.summary and a.summary.key_points
        ]
    industry_changes = [str(x) for x in industry_changes][:5]

    # —— 重点来源 ——
    source_map: dict[str, int] = {}
    for a in day_articles:
        source_map[a.source] = source_map.get(a.source, 0) + 1
    source_stats = sorted(
        ({"source": k, "count": v} for k, v in source_map.items()),
        key=lambda x: x["count"], reverse=True,
    )

    # —— 今日 AI 成本（ai_usage_log 聚合，当日 0-24 点 UTC，无数据为 0）——
    ai_cost = usage_service.usage_day_summary(db, day)

    return {
        "date": day.isoformat(),
        "issue_no": issue_no_for(day),
        "top_events": top_events,
        "quick_news": quick_news,
        "industry_changes": industry_changes,
        "source_stats": source_stats,
        "top_articles": top_articles,
        "max_hot_score": max_raw,
        "ai_cost": ai_cost,
    }


def generate_daily_report(db: Session, day: date_type | None = None) -> DailyReport:
    """生成某日日报并落库（content 为 JSONB 快照，生成后不随数据变化）。

    day 缺省取「昨日（UTC）」——最近一个已结束的自然日，保证窗口内有完整数据；
    当天数据须等次日生成或显式传 day 补算。
    """
    day = day or (datetime.now(timezone.utc) - timedelta(days=1)).date()
    # 同日并发生成（定时 + 手动触发）会撞 uq_report_date：会话级 advisory lock 串行化，
    # 后到者等待前者提交后走更新分支，双成功单行
    db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext('daily_report:' || :d))"), {"d": day.isoformat()})
    content = build_report(db, day)
    row = db.query(DailyReport).filter_by(date=day).first()
    if row:
        row.content = content
        row.issue_no = content["issue_no"]
    else:
        row = DailyReport(date=day, issue_no=content["issue_no"], content=content)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row
