"""早报服务（原「软件日报」，2026-09-17 改版）：每日 08:00 自动生成 / report_task 落库。

产品定位「今日早报」：早间速览「昨夜今晨」，统计窗口为
（发布日-1）00:00 → 发布日 08:00（北京时间）， overnight 报道不再顺延一天。
"""
import json
import re
import time
from collections import defaultdict
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai import llm_client
from app.algorithms.hot_score import hot_score
from app.core.config import get_settings
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.services import usage_service
from app.services.config_service import effective_llm
from app.services.report_push import push_enabled, push_report
from app.services.usage_service import record_usage
from app.utils.text import strip_media_lede, truncate, truncate_boundary
from app.utils.tz import bj_day_start, bj_now
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url
from sqlalchemy import text as sql_text
from sqlalchemy.orm import selectinload


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def issue_no_for(day: date_type) -> int:
    s = get_settings()
    start = datetime.fromisoformat(s.report_issue_epoch).date()
    return max(1, (day - start).days + 1)


# 早报统计窗口的今晨截稿时刻（发布日 08:00 北京时间）：与 beat 每日 08:00 的
# report_task 生成时刻对齐，overnight（00:00–08:00）报道归当期而非顺延一天；
# 窗口 [day-1 00:00, day 08:00) 内每篇报道恰好归属唯一一期，无缝无重叠
REPORT_WINDOW_END_HOUR = 8

# 早报「昨夜今晨热榜」条数（2026-09-19 由 TOP 5 扩为 TOP 10）：取窗口内
# 热度归一化后前 N 篇文章；「热点速览」取池起点随之同步后移，两栏目同页不重复
REPORT_TOP_ARTICLES = 10


# 转述句式「（科技媒体 X 发布博文，）报道称/指出/表示…」——变化本体在连接词之后
# （导语壳正则已提取至 utils/text.strip_media_lede，供早报速览说明复用）
_REPORTING_CONNECTOR_RE = re.compile(r'(?:报道称|博文称|视频称|报告中称|指出|表示|透露|分享了)')
_SENTENCE_SPLIT_RE = re.compile(r'[。！？]')


def _clean_change_text(raw: object) -> str:
    """行业变化文案清洗：剥离媒体导语壳 → 切断转述连接词 → 截取首句 → 限长。

    事件 impacts / 文章 key_points 里存的是原报道导语（"IT之家 9 月 8 日消息，
    科技媒体……发布博文，报道称……"），整条搬进「行业变化」栏目会整列同款开头；
    剥壳后真正承载"变化"的是转述连接词之后的本体。
    """
    text = strip_media_lede(str(raw or ""))
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
    """早报综述（无 LLM 的确定性模板）：头条事件 + 其余热点。

    早报于 08:00 发布、统计昨夜今晨（昨日 00:00 → 发布日 08:00），
    故措辞取「昨夜今晨最热」口径（与 36氪早报「昨夜今晨」定位一致）。
    生成链路先试 _llm_overview（配了 LLM 才调），任何失败回退本实现。
    （按需求不展示收录量与频道收录统计，channel_stats 参数保留仅为签名兼容。）
    """
    if articles_total <= 0:
        return ""
    parts: list[str] = []
    if top_events:
        e1 = top_events[0]
        lead = f"昨夜今晨最热「{truncate_boundary(e1['title'], 48)}」"
        if e1['articles_count'] > 1:
            lead += f"，聚合 {e1['articles_count']} 篇报道"
        if e1['sources_count'] > 1:
            lead += f"，{e1['sources_count']} 家来源交叉报道"
        parts.append(lead + "。")
        rest = [e for e in top_events[1:4]]
        if len(rest) == 1:
            parts.append(f"此外，「{truncate_boundary(rest[0]['title'], 32)}」亦在持续发酵。")
        elif rest:
            names = "、".join(f"「{truncate_boundary(e['title'], 32)}」" for e in rest)
            parts.append(f"此外，{names}等 {len(top_events) - 1} 个热点持续发酵。")
    return "".join(parts)


def _llm_overview(llm: tuple[str, str, str, str], content: dict, trigger: str = "auto") -> str | None:
    """LLM 撰写早报综述；未配置或任何失败返回 None（失败已落账），调用方回退模板。

    用量记账（task_type=report，成本看板「早报」类型）：HTTP 200 但 JSON 提取
    失败记 parse_fail（token 已花）并带纠错反馈自动重试一次；连接/非 200/URL
    守卫拦截记 api_fail（不重试）；未配置 LLM 时不调用不落账。
    与 event_service._llm_event_summary 同款「落账 + 降级」模式。
    """
    if not (llm and llm[0] and llm[1] and llm[2]):
        return None
    base_url, api_key, model, protocol = llm
    start = time.monotonic()

    def _record(status: str, error: str | None, **tokens) -> None:
        record_usage(
            task_type="report",
            model=model,
            status=status,
            error=error,
            latency_ms=int((time.monotonic() - start) * 1000),
            trigger=trigger,
            **tokens,
        )

    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        _record("api_fail", str(exc))
        return None

    events = content.get("top_events") or []
    quick = content.get("quick_news") or []
    ev_lines = "\n".join(
        f"- {e.get('title', '')}（{e.get('articles_count', 0)}篇/"
        f"{e.get('sources_count', 0)}源，{e.get('status', '')}）\n"
        f"  摘要：{truncate(str(e.get('summary') or ''), 80)}"
        for e in events[:5]
    )
    quick_lines = "；".join(
        f"{a.get('title', '')}（{a.get('source', '')}）" for a in quick[:8]
    )
    prompt = (
        "这是科技资讯「今日早报」的素材（早间 8 点发布，统计昨夜今晨："
        "昨日 00:00 → 今晨 08:00）。请写一段80~140字的中文早报综述："
        "概括昨夜今晨最热事件及其交叉报道情况、其他值得注意的热点。"
        "综述中不要出现收录报道总数、频道收录统计等收录量信息。"
        "要求：客观陈述事实、不加观点、不使用任何 markdown 标记、不要称呼与客套。\n"
        '输出严格 JSON：{"overview":"综述文本"}\n\n'
        f"热点事件：\n{ev_lines or '（无聚合事件）'}\n"
        f"其他热点：{quick_lines or '（无）'}"
    )
    data = None
    usage: dict = {}
    feedback = ""
    for attempt in range(2):
        try:
            raw, usage = llm_client.chat(
                base_url, api_key, model, protocol, prompt + feedback,
                temperature=0.3, json_mode=True,
            )
            data = llm_client.extract_json_object(raw)
            break
        except llm_client.LLMAPIError as exc:
            _record("api_fail", str(exc), **usage)
            return None
        except (ValueError, llm_client.LLMFormatError) as exc:
            # JSON 提取失败 / 响应结构异常：本次 token 如实落账（LLMFormatError
            # 的 usage 由异常带回，chat 未返回），末次失败后由调用方降级
            usage = getattr(exc, "usage", None) or usage
            _record("parse_fail", str(exc), **usage)
            feedback = llm_client.JSON_RETRY_HINT.format(error=exc)
    if data is None:
        return None
    overview = re.sub(r"\s+", " ", str(data.get("overview", ""))).strip()
    if not overview:
        # 空综述不落 success：token 已花按 parse_fail 入账，调用方回退模板
        _record("parse_fail", "empty overview", **usage)
        return None
    _record("success", None, **usage)
    return truncate(overview, 300)


def build_report(db: Session, day: date_type) -> dict:
    """构建早报内容（前端可直接渲染的结构）。

    day 为发布日（北京时间口径）：统计窗口取「昨夜今晨」——
    （day-1）00:00 → day 08:00 北京时间。每日 08:00（北京时间）自动生成
    当日发布期刊时窗口恰在生成时刻闭合；凌晨出现的外媒 overnight 消息
    归入当期早报，不再顺延一天。
    """
    win_start = bj_day_start(day - timedelta(days=1))
    win_end = bj_day_start(day) + timedelta(hours=REPORT_WINDOW_END_HOUR)

    win_articles = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .filter(Article.publish_time >= win_start, Article.publish_time < win_end)
        .all()
    )

    def with_score(a: Article):
        return a, hot_score(a.views, a.comments, a.shares, a.likes or 0, a.publish_time)

    scored = sorted(map(with_score, win_articles), key=lambda p: p[1], reverse=True)
    max_raw = scored[0][1] if scored else 0

    # —— TOP 事件（窗口内有报道的事件）——
    # 4 条集合查询替代逐事件 N+1（旧实现 = 1 + 2×事件总数 条查询）；
    # articles_count / sources_count 为早报窗口内统计（2026-09-09 审计修正：
    # 旧口径取事件全量成员，跨日事件会把历史文章计入"今日聚合 X 篇"），
    # 全生命周期数据放 lifecycle_* 字段单独呈现，两口径不混用
    win_ids = [a.id for a in win_articles]
    win_id_set = set(win_ids)
    events: list[HotEvent] = []
    member_ids_by_ev: dict[int, list[int]] = defaultdict(list)
    sources_by_ev: dict[int, set] = defaultdict(set)
    win_member_ids_by_ev: dict[int, list[int]] = defaultdict(list)
    win_sources_by_ev: dict[int, set] = defaultdict(set)
    if win_ids:
        candidate_ids = [
            row[0]
            for row in db.query(EventArticle.event_id)
            .filter(EventArticle.article_id.in_(win_ids))
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
                if aid in win_id_set:
                    win_member_ids_by_ev[eid].append(aid)
            art_sources = dict(
                db.query(Article.id, Article.source)
                .filter(Article.id.in_({aid for _, aid in links}))
                .all()
            )
            for eid, aid in links:
                src = art_sources.get(aid)
                if src:
                    sources_by_ev[eid].add(src)
                    if aid in win_id_set:
                        win_sources_by_ev[eid].add(src)
            events = db.query(HotEvent).filter(HotEvent.id.in_(candidate_ids)).all()

    top_events = []
    max_ev_raw = max((e.hot_score for e in events), default=0)
    for ev in events:
        win_member_ids = win_member_ids_by_ev.get(ev.id, [])
        top_events.append({
            "id": ev.id,
            "title": ev.title,
            "summary": ev.summary,
            "status": ev.status,
            "hot_score": round((ev.hot_score / max_ev_raw) * 100, 1) if max_ev_raw > 0 else round(ev.hot_score, 1),
            "articles_count": len(win_member_ids),
            "sources_count": len(win_sources_by_ev.get(ev.id, set())),
            "lifecycle_articles_count": len(member_ids_by_ev.get(ev.id, [])),
            "lifecycle_sources_count": len(sources_by_ev.get(ev.id, set())),
        })
    top_events = sorted(top_events, key=lambda e: e["hot_score"], reverse=True)[:5]

    # —— 热点速览 / 昨夜今晨热榜 TOP10：热度口径与全站对齐（榜单页均为 0-100
    #     归一化），基准取当日最大原始分 ——
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

    top_articles = [art_brief(a, hs) for a, hs in scored[:REPORT_TOP_ARTICLES]]
    # 热点速览 = 热榜 TOP10 之外、且未入 TOP 事件窗口成员的前 8 篇（2026-09-17
    # 早报算法改版；2026-09-19 热榜 TOP5 → TOP10，速览起点同步后移）：
    # 既不与「昨夜今晨热榜 TOP 10」同页两遍（旧 scored[:8] 口径问题），
    # 也不与头条/热点事件栏目重复同一事件——速览只呈现尚未被栏目覆盖的增量要闻
    featured_members: set[int] = set()
    for e in top_events:
        featured_members.update(win_member_ids_by_ev.get(e["id"], []))
    quick_pool = [
        (a, hs) for a, hs in scored[REPORT_TOP_ARTICLES:] if a.id not in featured_members
    ]
    quick_news = [art_brief(a, hs) for a, hs in quick_pool[:8]]

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
    for a in win_articles:
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

    # —— 早报窗口 AI 成本（与文章统计同一「昨夜今晨」窗口，无数据为 0）——
    ai_cost = usage_service.usage_window_summary(db, win_start, win_end)

    return {
        "date": day.isoformat(),
        "issue_no": issue_no_for(day),
        # 快照生成时刻：重新生成会覆盖，前端据此展示"生成于"
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # 早报综述（确定性模板；接入 LLM 后替换为 AI 撰写并以此兜底）
        "overview": _compose_overview(top_events, channel_stats, len(win_articles)),
        "top_events": top_events,
        "quick_news": quick_news,
        "industry_changes": industry_changes,
        "source_stats": source_stats,
        "channel_stats": channel_stats,
        "top_articles": top_articles,
        # TOP 事件的窗口成员文章 id（2026-09-19 飞书文本速览补位去重用：
        # 热榜补位时剔除，防同一资讯以「事件标题+成员文章标题」出现两遍）
        "featured_article_ids": sorted(featured_members),
        "articles_total": len(win_articles),
        "max_hot_score": max_raw,
        "ai_cost": ai_cost,
    }


def generate_daily_report(
    db: Session, day: date_type | None = None, trigger: str = "auto", allow_push: bool = True
) -> tuple[DailyReport, str]:
    """生成某日早报并落库，返回 (早报行, 推送状态串)。

    day 为发布日（北京时间口径），缺省取「今天」——每日 08:00（北京时间）
    自动生成当日发布期刊，统计窗口为昨夜今晨（昨日 00:00 → 发布日 08:00，
    build_report 内换算），08:00 触发时窗口恰在生成时刻闭合。

    综述两级实现（与全站「LLM + 兜底」同款）：配置了 LLM 先试 AI 撰写
    （overview_source=llm），未配置或失败回退确定性模板（overview_source=template）。

    推送策略（2026-09-10 审计收紧）：仅当发布日为「今天」（每日 08:00 自动口径的
    当期新刊，手动不传日期重算同属此列）且内容非空时才自动推送——补算历史日期
    不再向群通道灌旧闻/空报告；allow_push=False 供修复脚本重建当期刊时显式关闭
    推送；推送状态随返回值写入 task_run.detail，面板可见，不再静默丢弃。
    """
    day = day or bj_now().date()
    # 同日并发生成（定时 + 手动触发）会撞 uq_report_date：会话级 advisory lock 串行化，
    # 后到者等待前者提交后走更新分支，双成功单行
    db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext('daily_report:' || :d))"), {"d": day.isoformat()})
    content = build_report(db, day)
    if content["articles_total"] > 0:
        llm = effective_llm(db)
        llm_text = _llm_overview(llm, content, trigger=trigger)
        if llm_text:
            content["overview"] = llm_text
            content["overview_source"] = "llm"
        else:
            content["overview_source"] = "template"
    row = db.query(DailyReport).filter_by(date=day).first()
    if row:
        row.content = content
        row.issue_no = content["issue_no"]
    else:
        row = DailyReport(date=day, issue_no=content["issue_no"], content=content)
        db.add(row)
    db.commit()
    db.refresh(row)
    push_status = ""
    if push_enabled(db):
        if content["articles_total"] <= 0:
            push_status = "内容为空，未推送"
        elif not allow_push:
            push_status = "补算重建，未推送"
        elif day == bj_now().date():
            push_status = push_report(db, content)
        else:
            push_status = "历史日期补算，未推送"
    return row, push_status


def catchup_missing_reports(db: Session, now: datetime | None = None) -> list[date_type]:
    """找出应补生成的早报日期（本地 PC 部署错过 08:00 调度的补偿，2026-09-17 审计新增）。

    背景：本系统跑在用户本地 PC（docker compose），08:00 整点若关机/休眠/引擎停摆，
    report_task 不会补投（celery crontab 不追赶错过的调度），当天早报永久缺失且
    期号跳号（2026-09-16 第 155 期即因此缺失，告警 webhook 未配故无人知晓）。

    规则：
    - 历史日期回看 3 天（today-3/2/1，早于第 1 期基准日跳过）：无行即补；
    - 今天：仅过了今晨截稿时刻（08:00）仍无行才补——08:00 前生成会以不完整
      窗口顶出当期，且 day==today 的补算路径允许推送，不宜早于窗口闭合；
    - 已有行（含占位空刊）不重复生成：历史空刊说明窗口内本就无内容。
    返回待补日期（旧→新，期号随之升序）。
    """
    now = now or bj_now()
    today = now.date()
    epoch = datetime.fromisoformat(get_settings().report_issue_epoch).date()
    targets: list[date_type] = []
    for offset in (3, 2, 1):
        day = today - timedelta(days=offset)
        if day >= epoch:
            targets.append(day)
    if now.hour >= REPORT_WINDOW_END_HOUR:
        targets.append(today)
    return [d for d in targets if not db.query(DailyReport).filter_by(date=d).first()]
