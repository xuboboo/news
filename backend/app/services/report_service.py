"""日报服务（PRD 第 7 节）：每日自动生成 / report_task 落库。"""
import re
from collections import defaultdict
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.algorithms.hot_score import hot_score
from app.core.config import get_settings
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.services import usage_service
from app.services.report_push import push_enabled, push_report
from app.utils.text import truncate_boundary
from sqlalchemy import text as sql_text


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def issue_no_for(day: date_type) -> int:
    s = get_settings()
    start = datetime.fromisoformat(s.report_issue_epoch).date()
    return max(1, (day - start).days + 1)


# 「IT之家 9 月 8 日消息，」「快科技 9 月 9 日讯，」等媒体导语壳
_LEDE_PREFIX_RE = re.compile(
    r'^\S{0,12}\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?:消息|讯|快讯)\s*[，,]?\s*'
)
# 「据韩媒 sedaily 消息，」「据IT之家小伙伴反馈，」等无日期转述壳
_LEDE_JU_RE = re.compile(r'^据[^，。]{1,14}(?:消息|反馈|爆料)[，,]\s*')
# 转述句式「（科技媒体 X 发布博文，）报道称/指出/表示…」——变化本体在连接词之后
_REPORTING_CONNECTOR_RE = re.compile(r'(?:报道称|博文称|视频称|报告中称|指出|表示|透露|分享了)')
_SENTENCE_SPLIT_RE = re.compile(r'[。！？]')


def _clean_change_text(raw: object) -> str:
    """行业变化文案清洗：剥离媒体导语壳 → 切断转述连接词 → 截取首句 → 限长。

    事件 impacts / 文章 key_points 里存的是原报道导语（"IT之家 9 月 8 日消息，
    科技媒体……发布博文，报道称……"），整条搬进「行业变化」栏目会整列同款开头；
    剥壳后真正承载"变化"的是转述连接词之后的本体。
    """
    text = _LEDE_PREFIX_RE.sub('', str(raw or '').strip())
    text = _LEDE_JU_RE.sub('', text)
    if not text:
        return ''
    parts = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)
    first = parts[0] if parts and len(parts[0]) >= 8 else text
    # 连接词切断只在首句内做：跨句的"表示/指出"会劫持本来完好的短标题
    m = _REPORTING_CONNECTOR_RE.search(first[:60])
    if m and m.end() < len(first):
        candidate = first[m.end():].lstrip('，,：: ')
        if len(candidate) >= 8:
            first = candidate
    if len(first) > 110:
        # 超长时优先在句内最后一个逗号处截断，避免"将会基…"这类半字硬切
        cut = first.rfind('，', 60, 109)
        first = (first[:cut] if cut > 0 else first[:109]) + '…'
    return first


def _dedupe_texts(items: list[str]) -> list[str]:
    """按去标点后的前 24 字符近似去重（同一事件的导语变体高度雷同）。"""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = re.sub(r'\W+', '', it)[:24]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _compose_overview(
    top_events: list[dict],
    channel_stats: list[dict],
    articles_total: int,
) -> str:
    """今日综述（无 LLM 的确定性模板）：头条事件 + 其余热点 + 频道亮点。

    接入 LLM 后可在 build_report 里用同签名替换为 AI 撰写并以本实现兜底。
    """
    if articles_total <= 0:
        return ''
    parts: list[str] = []
    if top_events:
        e1 = top_events[0]
        lead = f"今日最热「{truncate_boundary(e1['title'], 48)}」"
        if e1['articles_count'] > 1:
            lead += f"，聚合 {e1['articles_count']} 篇报道"
        if e1['sources_count'] > 1:
            lead += f"，{e1['sources_count']} 家来源交叉报道"
        parts.append(lead + "。")
        rest = [e for e in top_events[1:4]]
        if rest:
            names = "、".join(f"「{truncate_boundary(e['title'], 32)}」" for e in rest)
            parts.append(f"此外，{names}等 {len(top_events) - 1} 个热点持续发酵。")
    if channel_stats:
        c1 = channel_stats[0]
        tail = f"{c1['channel']}以 {c1['count']} 篇收录居首"
        if len(channel_stats) > 1 and channel_stats[1]['count'] > 0:
            tail += f"，{channel_stats[1]['channel']} {channel_stats[1]['count']} 篇次之"
        parts.append(tail + "。")
    return "".join(parts)


def build_report(db: Session, day: date_type) -> dict:
    """构建日报内容（前端可直接渲染的结构）。"""
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    day_articles = (
        db.query(Article)
        .filter(Article.publish_time >= day_start, Article.publish_time < day_end)
        .all()
    )

    def with_score(a: Article):
        return a, hot_score(a.views, a.comments, a.shares, a.likes or 0, a.publish_time)

    scored = sorted(map(with_score, day_articles), key=lambda p: p[1], reverse=True)
    max_raw = scored[0][1] if scored else 0

    # —— TOP 事件（当天有报道的事件）——
    # 4 条集合查询替代逐事件 N+1（旧实现 = 1 + 2×事件总数 条查询）；
    # articles_count / sources_count 为日报窗口内统计（2026-09-09 审计修正：
    # 旧口径取事件全量成员，跨日事件会把历史文章计入"今日聚合 X 篇"），
    # 全生命周期数据放 lifecycle_* 字段单独呈现，两口径不混用
    day_ids = [a.id for a in day_articles]
    day_id_set = set(day_ids)
    events: list[HotEvent] = []
    member_ids_by_ev: dict[int, list[int]] = defaultdict(list)
    sources_by_ev: dict[int, set] = defaultdict(set)
    day_member_ids_by_ev: dict[int, list[int]] = defaultdict(list)
    day_sources_by_ev: dict[int, set] = defaultdict(set)
    if day_ids:
        candidate_ids = [
            row[0]
            for row in db.query(EventArticle.event_id)
            .filter(EventArticle.article_id.in_(day_ids))
            .distinct()
            .all()
        ]
        if candidate_ids:
            links = (
                db.query(EventArticle.event_id, EventArticle.article_id)
                .filter(EventArticle.event_id.in_(candidate_ids))
                .all()
            )
            for eid, aid in links:
                member_ids_by_ev[eid].append(aid)
                if aid in day_id_set:
                    day_member_ids_by_ev[eid].append(aid)
            art_sources = dict(
                db.query(Article.id, Article.source)
                .filter(Article.id.in_({aid for _, aid in links}))
                .all()
            )
            for eid, aid in links:
                src = art_sources.get(aid)
                if src:
                    sources_by_ev[eid].add(src)
                    if aid in day_id_set:
                        day_sources_by_ev[eid].add(src)
            events = db.query(HotEvent).filter(HotEvent.id.in_(candidate_ids)).all()

    top_events = []
    max_ev_raw = max((e.hot_score for e in events), default=0)
    for ev in events:
        day_member_ids = day_member_ids_by_ev.get(ev.id, [])
        top_events.append({
            "id": ev.id,
            "title": ev.title,
            "summary": ev.summary,
            "status": ev.status,
            "hot_score": round((ev.hot_score / max_ev_raw) * 100, 1) if max_ev_raw > 0 else round(ev.hot_score, 1),
            "articles_count": len(day_member_ids),
            "sources_count": len(day_sources_by_ev.get(ev.id, set())),
            "lifecycle_articles_count": len(member_ids_by_ev.get(ev.id, [])),
            "lifecycle_sources_count": len(sources_by_ev.get(ev.id, set())),
        })
    top_events = sorted(top_events, key=lambda e: e["hot_score"], reverse=True)[:5]

    # —— 热点速览 / TOP5：热度口径与全站对齐（榜单页均为 0-100 归一化），
    #     基准取当日最大原始分 ——
    def art_brief(a: Article, hs: float) -> dict:
        normalized = round((hs / max_raw) * 100, 1) if max_raw > 0 else round(hs, 1)
        return {
            "id": a.id,
            "title": a.title,
            "source": a.source,
            "channel": a.channel,
            "hot_score": normalized,
            "url": a.url,
            "publish_time": _tz(a.publish_time).isoformat(),
            "summary_short": a.summary.summary_short if a.summary else "",
            "tags": (a.summary.tags if a.summary else [])[:4],
        }

    # 热点速览 = TOP5 之后的 8 篇：收录量上来后与「当日热榜 TOP 5」零重叠
    # （旧口径 scored[:8] 时 TOP5 完全包含于速览，同页展示两遍）
    quick_news = [art_brief(a, hs) for a, hs in scored[5:13]]
    top_articles = [art_brief(a, hs) for a, hs in scored[:5]]

    # —— 行业变化：top 事件核心影响首条，剥导语壳 / 截首句 / 近似去重 ——
    ev_by_id = {ev.id: ev for ev in events}
    industry_changes = []
    for e in top_events:
        ev = ev_by_id.get(e["id"])
        if ev and ev.impacts:
            industry_changes.append(_clean_change_text(ev.impacts[0]))
    if not industry_changes:
        industry_changes = [
            _clean_change_text(a.summary.key_points[0])
            for a, _ in scored[:4]
            if a.summary and a.summary.key_points
        ]
    industry_changes = _dedupe_texts([x for x in industry_changes if x])[:5]

    # —— 重点来源：source 粒度只有 IT之家/快科技 两档（四频道共用 source），
    #     频道维度才有分布信息；source_stats 保留供旧快照/兼容 ——
    source_map: dict[str, int] = {}
    channel_map: dict[str, int] = {}
    for a in day_articles:
        source_map[a.source] = source_map.get(a.source, 0) + 1
        channel_map[a.channel] = channel_map.get(a.channel, 0) + 1
    source_stats = sorted(
        ({"source": k, "count": v} for k, v in source_map.items()),
        key=lambda x: x["count"], reverse=True,
    )
    channel_stats = sorted(
        ({"channel": k, "count": v} for k, v in channel_map.items()),
        key=lambda x: x["count"], reverse=True,
    )

    # —— 今日 AI 成本（ai_usage_log 聚合，当日 0-24 点 UTC，无数据为 0）——
    ai_cost = usage_service.usage_day_summary(db, day)

    return {
        "date": day.isoformat(),
        "issue_no": issue_no_for(day),
        # 快照生成时刻：重新生成会覆盖，前端据此展示"生成于"
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # 今日综述（确定性模板；接入 LLM 后替换为 AI 撰写并以此兜底）
        "overview": _compose_overview(top_events, channel_stats, len(day_articles)),
        "top_events": top_events,
        "quick_news": quick_news,
        "industry_changes": industry_changes,
        "source_stats": source_stats,
        "channel_stats": channel_stats,
        "top_articles": top_articles,
        "articles_total": len(day_articles),
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
    # 生成/重算后按配置推送（面板/env 双通道，默认关；失败不影响已落库的日报）
    if push_enabled(db):
        push_report(db, content)
    return row
