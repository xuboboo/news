"""事件服务：Embedding 聚类 → event / event_article 落库（PRD 第 5/9/10 节）。

增量逻辑：已有事件以质心参与聚类，新成员命中则并入；全新簇创建新事件。
事件总结与核心影响优先由 LLM 生成（配置后自动启用），否则由成员报道事实拼装。
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.ai import llm_client
from app.ai.embedding import centroid, cosine
from app.ai.event_cluster import (
    HIGH_BAND_KW_FLOOR,
    ClusterItem,
    cluster_articles,
    effective_thresholds,
    elect_title,
    event_time_range,
    event_title_for,
    events_mergeable,
    keyword_jaccard_of,
    similarity_gate_pass,
)
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.services import config_service
from app.services.usage_service import record_usage
from app.utils.text import extract_keywords, truncate, truncate_boundary
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# 仅识别"致谢行"类无效摘要（窄口径：正常导语如「IT之家 9 月 6 日消息」不算脏数据）
_DIRTY_SUMMARY = re.compile(r"线索投递|^(感谢|题图|配图|图片来源)")

# 事件吸纳年龄上限（天，2026-10-14 审计新增）：事件首个成员发布早于
# now - N 天即不再吸纳新簇、不参与事件级合并，冻结为存量展示。候选资格此前
# 只看 last_time 新鲜度，常青事件每天有新成员就永不结案，质心随成员增长泛化
# 成主题向量，同域文章全过门槛被吸入（实测 9/23「Win11 五周年」事件 21 天吞
# 93 篇混题成员、旧标题连续三日霸占早报头条）。窗口取 14 天对标 AIHOT 召回窗：
# 超龄后新文章另立新事件，标题随新文。存量超龄事件不删不改，榜单分随衰减自落。
EVENT_MAX_AGE_DAYS = 14

# 事件热度统计窗口（小时，2026-10-14 对齐 AIHOT 热榜「48h 硬窗口」口径）：
# recompute_event_scores 只聚合窗口内发布的成员文章，窗口滑出即归零沉底——
# 榜单反映「此刻谁在被讨论」，而非全生命周期衰减和（旧口径下冻结的杂烩事件
# 凭近期成员在热点事件榜长期占位）。与 EVENT_MAX_AGE_DAYS 分工：一个管榜单
# 新鲜度，一个管事件生长寿命。归一化展示基准取窗口内最高事件分，自适应。
EVENT_SCORE_WINDOW_HOURS = 48

# LLM「同一事件」审核门槛（2026-10-14 第三批，对标 AIHOT relate 四分类的简化版）：
# 目标事件成员数达到该值后，新簇并入前须过 LLM 判定——小事件自由成长零成本，
# 大事件成长须审核，掐断「质心吸得多 → 更糊 → 吸得更多」的雪球放大器
# （Win11 同域基线相似度 0.90+ 落在高位带，单一 embedding 门槛无法分离
# 同事件与同主题另一件事）。未配 LLM 或判定失败时不拦截（fail-open，与
# AIHOT「拿不准则合并」同取向，聚类不因 LLM 故障停摆）。
LLM_REVIEW_EVENT_MEMBERS = 5
# 仅「判定为另一件事且置信度不低于该值」才拦截并入；低置信度放行，防误杀拆散真同事件
LLM_REVIEW_MIN_CONFIDENCE = 0.7


def _summary_dirty(text: str | None) -> bool:
    return bool(text) and bool(_DIRTY_SUMMARY.search(text.strip()))


def _event_status(last_time: datetime) -> str:
    hours = (datetime.now(timezone.utc) - _tz(last_time)).total_seconds() / 3600
    if hours <= 6:
        return "发酵中"
    if hours <= 24:
        return "持续关注"
    return "已平息"


def _compose_summary(members: list[Article], anchor_title: str | None = None) -> str:
    """无 LLM 时的事件总结：纯事实拼装（来源 + 报道要点），不添加观点。

    anchor_title：事件当前标题。引子「围绕「X」」必须与展示标题同源——
    标题经选举可能与成员首篇不同，不传锚点会让面板上标题与总结各说各话
    （LLM 路径的 prompt 本就以事件标题为上下文，兜底路径此处对齐）。
    """
    members = [m for m in members if m]
    if len(members) == 1:
        a = members[0]
        if a.summary and a.summary.summary_short and not _summary_dirty(a.summary.summary_short):
            return a.summary.summary_short
        return a.title
    anchor = (anchor_title or "").strip() or members[0].title
    lines = []
    for a in members[:4]:
        lines.append(f"{a.source}报道：{truncate(a.title, 48)}")
    head = f"围绕「{truncate(anchor, 24)}」共有 {len(members)} 篇来自不同报道的关联文章："
    return head + "；".join(lines) + "。"


def _compose_impacts(members: list[Article]) -> list[str]:
    """核心影响：从各成员摘要的核心信息中取首条事实（有摘要时）。

    上限 110（而非更紧的显示位截断）：行业变化/事件页都要在此之上再做
    二次裁剪，写库阶段截太短会造成"期间透…"式的不可逆半句。
    """
    impacts = []
    for a in members:
        if a.summary and a.summary.key_points:
            impacts.append(truncate_boundary(a.summary.key_points[0], 110))
        if len(impacts) >= 3:
            break
    if not impacts:
        impacts = [truncate_boundary(a.title, 110) for a in members[:3]]
    return impacts


def _llm_event_summary(
    title: str,
    members: list[Article],
    llm: tuple[str, str, str, str] | None = None,
    event_id: int | None = None,
) -> tuple[str, list[str]] | None:
    """LLM 生成事件总结 + 核心影响；未配置或失败返回 None（失败已落账）。

    用量记账（task_type=event_summary）：成功记 success；HTTP 200 但 JSON 提取
    失败记 parse_fail（token 已花）并带纠错反馈自动重试一次；连接/非 200 记
    api_fail（不重试）。未配置 LLM 时不落账（降级拼装 _compose_summary 无
    API 调用）。event_id 为空时（新建事件、尚未 flush）流水只带标题上下文。
    """
    if not llm or not (llm[0] and llm[1] and llm[2]):
        return None
    base_url, api_key, model, protocol = llm
    start = time.monotonic()

    def _record(status: str, error: str | None, **tokens) -> None:
        record_usage(
            task_type="event_summary",
            model=model,
            status=status,
            error=error,
            latency_ms=int((time.monotonic() - start) * 1000),
            event_id=event_id,
            **tokens,
        )

    try:
        base_url = assert_public_http_url(base_url)  # 违规 → api_fail 落账后降级事实拼装
    except UrlNotAllowed as exc:
        _record("api_fail", str(exc))
        return None

    material = "\n".join(
        f"- [{a.source}] {a.title}\n  摘要：{a.summary.summary_short if a.summary else ''}"
        for a in members[:6]
    )
    prompt = (
        "以下是一个科技热点事件的多篇报道。请输出严格 JSON："
        '{"summary":"事件总结(150字内,客观,只保留事实)","impacts":["核心影响1","核心影响2","核心影响3"]}\n\n'
        f"事件标题：{title}\n报道：\n{material}"
    )
    data = None
    usage: dict = {}
    feedback = ""
    for attempt in range(2):
        try:
            raw, usage = llm_client.chat(
                base_url, api_key, model, protocol, prompt + feedback,
                temperature=0.2, json_mode=True,
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
    raw_impacts = data.get("impacts")
    summary = re.sub(r"\s+", " ", str(data.get("summary", ""))).strip()
    if not summary:
        # 空总结不落 success（同 _llm_overview 的 empty overview 口径）：
        # token 已花按 parse_fail 入账，调用方降级事实拼装
        _record("parse_fail", "empty summary", **usage)
        return None
    _record("success", None, **usage)
    return (
        truncate(summary, 300),
        # 容器类型守卫：impacts 非 list（字符串/dict）时按缺失处理，
        # 降级事实拼装（同 summary.key_points 的防线）
        [str(i) for i in raw_impacts][:4] if isinstance(raw_impacts, list) else [],
    )


def build_relation_prompt(
    event_title: str,
    event_summary: str | None,
    member_titles: list[str],
    new_titles: list[str],
) -> str:
    """LLM「同一事件」判定提示词（纯函数，与 parse_relation_verdict 配对）。"""
    members = "\n".join(f"- {t}" for t in member_titles[:8]) or "-（无）"
    fresh = "\n".join(f"- {t}" for t in new_titles[:4]) or "-（无）"
    return (
        "判定新闻事件归属。【新报道】描述的是否就是【已有事件】本身（含其直接后续进展，"
        "如官方回应、评测、细节披露）？\n"
        "判定口径：同一事件 = 同一次现实发生（同一发布 / 同一更新 / 同一判决 / 同一事故）；\n"
        "另一件事 = 同一公司、产品或主题下的其他新闻——例如都关于 Win11，一件是五周年回顾、"
        "另一件是补丁修复，即为另一件事。拿不准时判同一事件。\n"
        "输出严格 JSON：{\"same_event\": true或false, \"confidence\": 0到1, \"reason\": \"一句话理由\"}\n\n"
        f"【已有事件】{event_title}\n"
        f"事件摘要：{truncate(str(event_summary or ''), 200) or '（无）'}\n"
        f"近期成员报道：\n{members}\n\n"
        f"【新报道】\n{fresh}"
    )


def parse_relation_verdict(raw: object) -> tuple[bool, float] | None:
    """解析 LLM 判定 JSON（纯函数）：返回 (same_event, confidence)；结构无效返回 None。

    宽松口径：confidence 缺失或非数值按 0.5 处理（低于拦截线 → 放行，fail-open）。
    """
    if not isinstance(raw, dict):
        return None
    same = raw.get("same_event")
    if not isinstance(same, bool):
        return None
    try:
        conf = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    if conf != conf or conf < 0:  # NaN / 负数防御
        conf = 0.5
    return same, min(conf, 1.0)


def _llm_same_event(
    event_title: str,
    event_summary: str | None,
    member_titles: list[str],
    new_titles: list[str],
    llm: tuple[str, str, str, str],
    event_id: int | None = None,
) -> bool | None:
    """LLM「同一事件」判定：True=同一事件可并；False=另一件事应拦；
    None=失败（调用方放行，fail-open）。

    用量记账（task_type=event_group）：与 _llm_event_summary 同款「落账 + 降级」
    模式——成功记 success；JSON 提取失败记 parse_fail 并带纠错反馈重试一次；
    连接/非 200/URL 守卫拦截记 api_fail 不重试。
    """
    base_url, api_key, model, protocol = llm
    start = time.monotonic()

    def _record(status: str, error: str | None, **tokens) -> None:
        record_usage(
            task_type="event_group",
            model=model,
            status=status,
            error=error,
            latency_ms=int((time.monotonic() - start) * 1000),
            event_id=event_id,
            **tokens,
        )

    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        _record("api_fail", str(exc))
        return None

    prompt = build_relation_prompt(event_title, event_summary, member_titles, new_titles)
    data = None
    usage: dict = {}
    feedback = ""
    for attempt in range(2):
        try:
            raw, usage = llm_client.chat(
                base_url, api_key, model, protocol, prompt + feedback,
                temperature=0, json_mode=True,
            )
            data = llm_client.extract_json_object(raw)
            break
        except llm_client.LLMAPIError as exc:
            _record("api_fail", str(exc), **usage)
            return None
        except (ValueError, llm_client.LLMFormatError) as exc:
            usage = getattr(exc, "usage", None) or usage
            _record("parse_fail", str(exc), **usage)
            feedback = llm_client.JSON_RETRY_HINT.format(error=exc)
    if data is None:
        return None
    verdict = parse_relation_verdict(data)
    if verdict is None:
        _record("parse_fail", "invalid relation verdict", **usage)
        return None
    _record("success", None, **usage)
    same, conf = verdict
    if not same and conf < LLM_REVIEW_MIN_CONFIDENCE:
        return None  # 低置信度的「另一件事」不放拦，放行并入
    return same


def _event_member_titles(db: Session, event_id: int, limit: int = 8) -> list[str]:
    """事件最近成员的标题（LLM 审核上下文用，新→旧截前 limit 条）。"""
    rows = (
        db.query(Article.title)
        .join(EventArticle, EventArticle.article_id == Article.id)
        .filter(EventArticle.event_id == event_id)
        .order_by(Article.publish_time.desc())
        .limit(limit)
        .all()
    )
    return [str(t) for (t,) in rows if t]


def _recompute_centroid(db: Session, event_id: int) -> list[float] | None:
    """事件质心 = 全量成员向量的归一化均值。增删成员后重算，不做增量近似
    （近似会把质心偏向最近并入的少数文章，累积漂移后相似度判定失真）。"""
    rows = (
        db.query(Article.embedding)
        .join(EventArticle, EventArticle.article_id == Article.id)
        .filter(EventArticle.event_id == event_id)
        .all()
    )
    vecs = [list(r[0]) for r in rows if r[0] is not None]
    return centroid(vecs) if vecs else None


def _event_members(db: Session, event_id: int) -> list[Article]:
    """按发布时间升序装载事件全量成员文章。"""
    return (
        db.query(Article)
        .join(EventArticle, EventArticle.article_id == Article.id)
        .filter(EventArticle.event_id == event_id)
        .order_by(Article.publish_time)
        .all()
    )


def repair_fallback_event_summaries(db: Session, within_hours: int = 24, limit: int = 3) -> int:
    """事件总结降级自愈：重算「总结仍是事实拼装且 LLM 现已配置」的近期事件。

    场景：事件总结的 LLM 调用重试仍失败（api_fail/截断，2026-09-12 事件5 实测）
    时永久停留 _compose_summary 事实拼装，只有成员再变动才会重建——静止事件
    无人补上。本函数随 event_task（每 10 分钟）触发：以「围绕「」拼装引子为
    签名识别降级事件（多成员拼装路径的固定开头；单成员路径回退摘要正文，
    该形态不在此自愈范围），限近 within_hours 且单轮限量，成本有硬上界；
    LLM 成功后签名消失自然收敛。

    返回重算的事件数。
    """
    if not all(config_service.effective_llm(db)[:3]):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    events = (
        db.query(HotEvent)
        .filter(
            HotEvent.summary.like("围绕「%"),
            HotEvent.last_time >= cutoff,
        )
        .order_by(HotEvent.last_time.desc())
        .limit(limit)
        .all()
    )
    rebuilt = 0
    for ev in events:
        members = _event_members(db, ev.id)
        if not members:
            continue
        _rebuild_event_content(db, ev, members)
        rebuilt += 1
    if rebuilt:
        db.commit()
    return rebuilt


def _rebuild_event_content(db: Session, ev: HotEvent, members: list[Article]) -> None:
    """重算事件标题 / 总结 / 核心影响（LLM 优先，失败降级事实拼装）并重建时间线。

    标题随成员重选举（与建簇 event_title_for 同口径）：旧实现只在创建时定标题，
    事件从单篇长成几十篇后仍顶着最初那篇的表述。选举先于 LLM 总结，总结拿到
    最新标题上下文。
    """
    ev.title = elect_title(
        [m.title for m in members],
        [set(extract_keywords(m.title or "", 10)) for m in members],
    )
    # LLM 配置运行时可改（管理面板），每次重算读取生效值；未配置/失败在
    # _llm_event_summary 内落账并返回 None，降级事实拼装（业务零感知）。
    # 此处曾是漏传 llm 的死代码——设计上的唯一事件 LLM 调用点自创建起从未生效。
    llm = _llm_event_summary(
        ev.title, members, llm=config_service.effective_llm(db), event_id=ev.id,
    )
    ev.summary = llm[0] if llm else _compose_summary(members, anchor_title=ev.title)
    if llm:
        # LLM 只给了总结没给 impacts 时，核心影响回落事实拼装，不空着
        ev.impacts = llm[1] or _compose_impacts(members)
    ev.timeline = [
        {
            "time": _tz(a.publish_time).isoformat(),
            "text": truncate(a.summary.summary_short, 60) if a.summary else truncate(a.title, 60),
            "source": a.source,
        }
        for a in sorted(members, key=lambda x: x.publish_time, reverse=True)
    ]


def _absorb_event(db: Session, winner: HotEvent, loser: HotEvent) -> None:
    """把 loser 事件并入 winner：迁移成员关联（撞 uq_event_article 的先删）、
    扩展时间跨度、重算质心与展示内容，最后删除 loser。"""
    winner_ids = {
        lnk.article_id for lnk in db.query(EventArticle).filter_by(event_id=winner.id).all()
    }
    for lnk in db.query(EventArticle).filter_by(event_id=loser.id).all():
        if lnk.article_id in winner_ids:
            db.delete(lnk)
        else:
            lnk.event_id = winner.id
    # autoflush=False：迁移后的成员关联必须先落库，下面的质心重算才能看到
    # 全量成员——否则查询仍按旧关联算，质心漏掉迁移成员并写库，持续到下次
    # 成员变动才被自愈（2026-09-10 集成测试实测捕获）
    db.flush()
    if winner.first_time and loser.first_time:
        winner.first_time = min(winner.first_time, loser.first_time, key=_tz)
    if winner.last_time and loser.last_time:
        winner.last_time = max(winner.last_time, loser.last_time, key=_tz)
    winner.status = _event_status(winner.last_time)
    new_centroid = _recompute_centroid(db, winner.id)
    if new_centroid is not None:
        winner.centroid = new_centroid
    db.flush()
    members = _event_members(db, winner.id)
    if members:
        _rebuild_event_content(db, winner, members)
    db.delete(loser)
    db.flush()


def _merge_recent_events(
    db: Session,
    *,
    since: datetime,
    sim_threshold: float,
    keyword_threshold: float,
    window_hours: int,
    high_band_kw_floor: float = HIGH_BAND_KW_FLOOR,
    llm: tuple[str, str, str, str] = ("", "", "", ""),
) -> int:
    """事件级二阶段合并：先做共享成员去重（确定性同事件，不依赖相似度），
    再对剩余事件两两做同门槛判定（质心相似度 + 标题关键词 + 来源交叉 + 时间
    跨度相邻），命中则成员多者吞并少者。循环至不再有可合并对（上限 50 轮，
    防病态数据死循环）。返回合并次数。

    超龄事件（首个成员早于 EVENT_MAX_AGE_DAYS）不参与合并：防止「老事件吞并
    新事件」把新文章卷回旧杂烩——合并后 first_time 取 min，结果仍是超龄事件，
    与吸纳年龄上限同一语义（见 run_event_clustering 内注释）。

    ② 相似度合并受 LLM「同一事件」审核（2026-10-14 第三批）：任一方成员数达
    LLM_REVIEW_EVENT_MEMBERS 即交判定，判「另一件事」则跳过该对——与 step 4 的
    成员采纳审核同一语义：两个各自长大的事件合并成更大的事件，同样须过审。
    ① 共享成员去重不受审（一篇文章挂两事件是确定性同事件证据，幂等修复）。
    被拒对记入 refused_pairs 防重扫死循环。"""
    events = (
        db.query(HotEvent)
        .filter(
            HotEvent.last_time >= since - timedelta(hours=24),
            HotEvent.first_time >= datetime.now(timezone.utc) - timedelta(days=EVENT_MAX_AGE_DAYS),
        )
        .all()
    )
    if len(events) < 2:
        return 0
    kw_map = {ev.id: set(extract_keywords(ev.title or "", 10)) for ev in events}
    src_rows = (
        db.query(EventArticle.event_id, Article.source)
        .join(Article, Article.id == EventArticle.article_id)
        .filter(EventArticle.event_id.in_([e.id for e in events]))
        .all()
    )
    src_map: dict[int, set[str]] = {}
    for eid, source in src_rows:
        src_map.setdefault(eid, set()).add(source or "")
    sizes: dict[int, int] = dict(
        db.query(EventArticle.event_id, func.count(EventArticle.article_id))
        .filter(EventArticle.event_id.in_([e.id for e in events]))
        .group_by(EventArticle.event_id)
        .all()
    )

    merged = 0

    # ① 共享成员去重：同一篇文章挂在多个事件下（step-2 成员重叠救援的历史副作用）
    #    即确定性同事件，直接并入成员多者——不依赖相似度（质心可能尚未重建）
    by_article: dict[int, set[int]] = {}
    for aid, eid in (
        db.query(EventArticle.article_id, EventArticle.event_id)
        .filter(EventArticle.event_id.in_([e.id for e in events]))
        .all()
    ):
        by_article.setdefault(aid, set()).add(eid)
    shared = {aid: eids for aid, eids in by_article.items() if len(eids) > 1}
    if shared:
        alive = {ev.id: ev for ev in events}

        def _winner_of(eids: set[int]) -> HotEvent:
            return max(
                (alive[e] for e in eids if e in alive),
                key=lambda ev: (sizes.get(ev.id, 0), -ev.id),
            )

        for aid in sorted(shared):
            eids = {e for e in shared[aid] if e in alive}
            if len(eids) < 2:
                continue
            winner = _winner_of(eids)
            for loser in list(alive.values()):
                if loser.id in eids and loser.id != winner.id:
                    _absorb_event(db, winner, loser)
                    kw_map[winner.id] = set(extract_keywords(winner.title or "", 10))
                    sizes[winner.id] = sizes.get(winner.id, 0) + sizes.get(loser.id, 0)
                    src_map[winner.id] = src_map.get(winner.id, set()) | src_map.get(loser.id, set())
                    alive.pop(loser.id)
                    merged += 1
        events = [ev for ev in alive.values()]

    def _mergeable(a: HotEvent, b: HotEvent) -> bool:
        return events_mergeable(
            list(a.centroid) if a.centroid is not None else None,
            list(b.centroid) if b.centroid is not None else None,
            kw_map[a.id],
            kw_map[b.id],
            src_map.get(a.id, set()),
            src_map.get(b.id, set()),
            a.first_time,
            a.last_time,
            b.first_time,
            b.last_time,
            sim_threshold=sim_threshold,
            keyword_threshold=keyword_threshold,
            window_hours=window_hours,
            high_band_kw_floor=high_band_kw_floor,
        )

    # ② 相似度合并：50 轮上限覆盖 ①+② 的总合并数，防病态数据死循环
    refused_pairs: set[tuple[int, int]] = set()
    while merged < 50:
        pair = None
        for i, a in enumerate(events):
            for b in events[i + 1:]:
                if _mergeable(a, b) and (a.id, b.id) not in refused_pairs:
                    pair = (a, b)
                    break
            if pair:
                break
        if pair is None:
            break
        a, b = pair
        # 成员多者为 winner（平票取先创建的 id 小者，保证确定性）
        winner, loser = (
            (a, b)
            if (sizes.get(a.id, 0), -a.id) >= (sizes.get(b.id, 0), -b.id)
            else (b, a)
        )
        # LLM「同一事件」审核（大事件吞并前）：判「另一件事」则跳过该对
        if llm[0] and llm[1] and llm[2] and max(
            sizes.get(winner.id, 0), sizes.get(loser.id, 0)
        ) >= LLM_REVIEW_EVENT_MEMBERS:
            ok = _llm_same_event(
                winner.title or "",
                winner.summary,
                _event_member_titles(db, winner.id),
                [loser.title] if loser.title else [],
                llm,
                event_id=winner.id,
            )
            if ok is False:
                refused_pairs.add((a.id, b.id))
                continue
        _absorb_event(db, winner, loser)
        kw_map[winner.id] = set(extract_keywords(winner.title or "", 10))
        sizes[winner.id] = sizes.get(winner.id, 0) + sizes.get(loser.id, 0)
        src_map[winner.id] = src_map.get(winner.id, set()) | src_map.get(loser.id, set())
        events = [ev for ev in events if ev.id != loser.id]
        merged += 1
    return merged


def run_event_clustering(db: Session) -> dict:
    """对近期已向量化文章执行聚类并增量更新事件库。返回统计。"""
    # 阈值/回看窗读运行时生效值（管理面板可热调，DB 覆盖 .env）
    sim_threshold = float(config_service.get_effective(db, "sim_threshold"))
    keyword_threshold: float | None = None
    high_band_kw_floor: float = HIGH_BAND_KW_FLOOR
    window_hours: int | None = None
    try:
        keyword_threshold = float(config_service.get_effective(db, "keyword_jaccard_threshold"))
        window_hours = int(config_service.get_effective(db, "cluster_window_hours"))
    except KeyError:
        pass  # 面板白名单未放行的键回落 .env 静态值（effective_thresholds 兜底）
    try:
        high_band_kw_floor = float(config_service.get_effective(db, "high_band_kw_floor"))
    except KeyError:
        pass
    lookback_hours = int(config_service.get_effective(db, "cluster_lookback_hours"))
    sim_threshold, keyword_threshold, window_hours = effective_thresholds(
        sim_threshold, keyword_threshold, window_hours
    )
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    articles = (
        db.query(Article)
        .filter(Article.publish_time >= since, Article.embedding.isnot(None))
        .order_by(Article.publish_time.desc())
        .all()
    )
    if not articles:
        return {"clusters": 0, "events_updated": 0, "events_created": 0}

    items = [
        ClusterItem(
            id=a.id,
            title=a.title,
            embedding=list(a.embedding),
            publish_time=a.publish_time,
            channel=a.channel,
            source=a.source,
        )
        for a in articles
    ]
    clusters = cluster_articles(
        items,
        sim_threshold=sim_threshold,
        keyword_threshold=keyword_threshold,
        window_hours=window_hours,
        high_band_kw_floor=high_band_kw_floor,
    )
    article_map = {a.id: a for a in articles}

    # 候选事件及其来源/关键词映射一轮构建一次（旧实现在簇循环内每簇重查重算：
    # O(簇数) 次重复查询 + 每簇对全部候选事件重跑 jieba）。当轮新建/更新的事件
    # 在下方原地维护这三个结构，保持与逐簇重查完全相同的可见性。
    # 吸纳年龄上限（2026-10-14 审计新增，对标 AIHOT 14 天召回窗）：候选资格除
    # last_time 新鲜外，还要求 first_time 在 EVENT_MAX_AGE_DAYS 内——否则常青
    # 事件每天有新成员就永不结案，质心随成员增长泛化成主题向量，同域文章
    # （Win11 系列同域基线 0.90+ 全过门槛）持续被吸入：实测 9/23「Win11 五周年」
    # 事件 21 天吞 93 篇混题成员、旧标题连续三日霸占早报头条。超龄事件冻结为
    # 存量展示，新文章另立新事件、标题随新文。
    horizon = since - timedelta(hours=24)
    age_horizon = datetime.now(timezone.utc) - timedelta(days=EVENT_MAX_AGE_DAYS)
    candidate_events = (
        db.query(HotEvent)
        .filter(HotEvent.last_time >= horizon, HotEvent.first_time >= age_horizon)
        .all()
    )
    sources_by_ev: dict[int, set[str]] = {}
    if candidate_events:
        for eid, src in (
            db.query(EventArticle.event_id, Article.source)
            .join(Article, Article.id == EventArticle.article_id)
            .filter(EventArticle.event_id.in_([e.id for e in candidate_events]))
            .all()
        ):
            if src:
                sources_by_ev.setdefault(eid, set()).add(src)
    kw_by_ev = {
        ev.id: set(extract_keywords(ev.title or "", 10)) for ev in candidate_events
    }

    # LLM「同一事件」审核用：生效配置每轮取一次（未配置为空串四元组，钩子自关）
    llm = config_service.effective_llm(db)

    updated = created = 0
    window = timedelta(hours=window_hours)
    for cluster in clusters:
        member_articles = [article_map[m.id] for m in cluster.members]

        # 1) 尝试并入已有事件：与簇内合并完全同口径的三段式门槛
        #    （similarity_gate_pass：带级相似度 + 关键词 + 跨来源），另加时间窗相邻校验。
        #    旧实现只比标题关键词、无相似度下限；其后的"成员重叠救援"分支更是
        #    零门槛并入——审计实测（2026-09-09）污染出 20+ 篇混题事件。
        #    多个过门候选取相似度最高者（而非首条命中），归属不依赖查询顺序。
        rep = cluster.members[0]
        cluster_times = [_tz(m.publish_time) for m in cluster.members]
        c_first, c_last = min(cluster_times), max(cluster_times)

        target_event: HotEvent | None = None
        best_sim = 0.0
        for ev in candidate_events:
            # 时间窗：事件区间 [first, last] 与簇区间间隔超过聚类窗口则不相邻
            # （与 events_mergeable 同口径）
            if ev.last_time is None or ev.first_time is None:
                continue
            gap = max(_tz(ev.first_time), c_first) - min(_tz(ev.last_time), c_last)
            if gap > window:
                continue
            ev_vec = list(ev.centroid) if ev.centroid is not None else None
            sim = max(
                (cosine(list(m.embedding), ev_vec) for m in cluster.members),
                default=0.0,
            )
            kw = keyword_jaccard_of(rep.kw_set, kw_by_ev.get(ev.id, set()))
            if sim > best_sim and similarity_gate_pass(
                sim,
                kw,
                cluster.sources,
                sources_by_ev.get(ev.id, set()),
                sim_threshold,
                keyword_threshold,
                high_band_kw_floor,
            ):
                best_sim, target_event = sim, ev

        if target_event is None:
            # 2) 成员重叠救援（幂等守卫）：增量重聚类会把已归属某事件的文章重新成簇，
            #    完全不认领会重复建事件。但救援必须守住语义下限——目标事件的 max
            #    成员相似度 ≥ sim_threshold（不要求关键词）。旧版零门槛认领正是
            #    混题事件越长越大的放大器：大杂烩质心吸得多 → 质心更糊 → 吸得更多。
            #    目标按 (重叠数, 相似度) 取最优；都不达标则落入新建事件。
            member_ids = [m.id for m in cluster.members]
            existing_links = (
                db.query(EventArticle)
                .filter(EventArticle.article_id.in_(member_ids))
                .all()
            )
            if existing_links:
                overlap: dict[int, int] = {}
                for lnk in existing_links:
                    overlap[lnk.event_id] = overlap.get(lnk.event_id, 0) + 1
                rescue_key: tuple[int, float] | None = None
                for eid in overlap:
                    ev = db.get(HotEvent, eid)
                    if ev is None or ev.centroid is None:
                        continue
                    # 时间窗相邻校验与 step 1 / events_mergeable 同口径：
                    # 救援只该「认领回家的成员」，不该跨时间窗吸附远期事件
                    if ev.first_time is None or ev.last_time is None:
                        continue
                    ev_first, ev_last = _tz(ev.first_time), _tz(ev.last_time)
                    gap = max(_tz(rep.publish_time), ev_first) - min(
                        min(cluster_times), ev_last
                    )
                    if gap > window:
                        continue
                    ev_vec = list(ev.centroid)
                    sim = max(
                        (cosine(list(m.embedding), ev_vec) for m in cluster.members),
                        default=0.0,
                    )
                    if sim < sim_threshold:
                        continue
                    key = (overlap[eid], sim)
                    if rescue_key is None or key > rescue_key:
                        rescue_key = key
                        target_event = ev

        if target_event is None:
            # 3) 新建事件（总结先用事实拼装兜底；成员关联建立后第 4 步统一重算一次。
            #    旧实现此处调一次 LLM、第 4 步重算再调一次，每个新建事件白花一倍 token）
            title = event_title_for(cluster.members)
            first_time, last_time = event_time_range(cluster.members)
            target_event = HotEvent(
                title=title,
                summary=_compose_summary(member_articles, anchor_title=title),
                impacts=_compose_impacts(member_articles),
                first_time=first_time,
                last_time=last_time,
                status=_event_status(last_time),
                centroid=cluster.centroid,
                timeline=[],
            )
            db.add(target_event)
            db.flush()
            # 当轮后续簇立即可见（等价旧实现的逐簇重查）
            candidate_events.append(target_event)
            kw_by_ev[target_event.id] = set(extract_keywords(target_event.title or "", 10))
            sources_by_ev[target_event.id] = set(cluster.sources)
            created += 1
        else:
            updated += 1
            # 更新标题为更核心的表述、时间与状态
            first_time, last_time = event_time_range(cluster.members)
            if _tz(last_time) > _tz(target_event.last_time or last_time):
                target_event.last_time = last_time
            if target_event.first_time is None or _tz(first_time) < _tz(target_event.first_time):
                target_event.first_time = first_time
            target_event.status = _event_status(target_event.last_time)

        # 4) 维护成员关联与时间线
        member_ids = {m.id for m in cluster.members}
        linked = {
            lnk.article_id
            for lnk in db.query(EventArticle).filter_by(event_id=target_event.id).all()
        }
        new_links = member_ids - linked
        # LLM「同一事件」审核（2026-10-14 第三批，唯一的成员采纳收口点）：
        # embedding 门槛无法分离同事件与同主题另一件事（Win11 同域基线 0.90+ 落在
        # 高位带），事件存量成员达 LLM_REVIEW_EVENT_MEMBERS 后，新增成员并入前交
        # LLM 判定——判「另一件事」且置信度达标则拒绝本轮新增（存量成员照旧归位，
        # 幂等；被拒文章保持未聚类，下轮重聚类复审）。审核放收口点而非 step 1：
        # 新文章会与事件的旧成员聚成同一簇、经救援分支「认领回家」绕过准入审核，
        # 只有这里能同时拦住 step 1 与救援两条并入路径。未配 LLM 或判定失败一律
        # 放行（fail-open，聚类不因 LLM 故障停摆）；小事件不审核零成本。
        if (
            new_links
            and llm[0] and llm[1] and llm[2]
            and len(linked) >= LLM_REVIEW_EVENT_MEMBERS
        ):
            fresh_titles = [
                a.title for a in member_articles if a.id in new_links and a.title
            ]
            ok = _llm_same_event(
                target_event.title or "",
                target_event.summary,
                _event_member_titles(db, target_event.id),
                fresh_titles,
                llm,
                event_id=target_event.id,
            )
            if ok is False:
                new_links = set()
        for aid in new_links:
            db.add(EventArticle(event_id=target_event.id, article_id=aid))
        if new_links or target_event.centroid is None:
            # autoflush=False：显式 flush 让后续查询立刻看到新关联——否则本轮
            # 质心重算漏掉新成员、二阶段合并的共享成员去重也认不出当轮新建事件
            # （曾致质心滞后一轮、重复事件多活一个周期）
            db.flush()
            # AI 状态机最后一环：文章已归入事件（pending → summarized → embedded → clustered）
            if new_links:
                for m in cluster.members:
                    a = article_map.get(m.id)
                    if a is not None and a.ai_status != "clustered":
                        a.ai_status = "clustered"
            # 成员集合变化或质心缺失（维度迁移清空/历史残留）时，由全量成员向量重算
            # （旧实现直接用本轮簇质心覆盖：单文章簇会把多成员事件的质心拉偏成
            # 最后一篇并入文章的向量，后续轮次判相似度失真）
            new_centroid = _recompute_centroid(db, target_event.id)
            if new_centroid is not None:
                target_event.centroid = new_centroid
            # 新成员来源并入候选映射（等价旧实现重查 event_article；集合并集不重不漏）
            sources_by_ev[target_event.id] = (
                sources_by_ev.get(target_event.id, set()) | cluster.sources
            )

        # 成员变化或存量摘要为套话（致谢行等）时，重算事件总结 / 核心影响 / 时间线，
        # 保证 summary 中的关联文章数始终与 event_article 实际成员数一致
        if new_links or _summary_dirty(target_event.summary):
            all_ids = [
                lnk.article_id
                for lnk in db.query(EventArticle).filter_by(event_id=target_event.id).all()
            ]
            all_members = []
            dead_ids: set[int] = set()
            for aid in all_ids:
                m = article_map.get(aid) or db.get(Article, aid)
                if m:
                    all_members.append(m)
                else:
                    dead_ids.add(aid)
            if dead_ids:
                # 死链清理：成员关联指向已不存在的文章（历史数据残留），删掉后重查
                db.query(EventArticle).filter(
                    EventArticle.event_id == target_event.id,
                    EventArticle.article_id.in_(dead_ids),
                ).delete(synchronize_session=False)
                db.flush()
                all_ids = [
                    lnk.article_id
                    for lnk in db.query(EventArticle).filter_by(event_id=target_event.id).all()
                ]
                all_members = sorted(
                    (m for m in (article_map.get(a) or db.get(Article, a) for a in all_ids) if m),
                    key=lambda x: x.publish_time,
                )

            if all_members:
                all_members.sort(key=lambda x: x.publish_time)
                _rebuild_event_content(db, target_event, all_members)
                db.flush()
                # 标题可能已随成员重选举：候选事件的关键词映射同步刷新
                kw_by_ev[target_event.id] = set(
                    extract_keywords(target_event.title or "", 10)
                )
            # all_members 为空（成员全部失效）时仅清理死链并保留原摘要，不再重算

    # 5) 二阶段合并：事件间的同门槛两两归并，修复"同事件文章跨轮次先后到达、
    #    各自成事件后不再合并"的拆分（建簇只在单轮文章间判定，事件库内无合并机会）
    merged = _merge_recent_events(
        db,
        since=since,
        sim_threshold=sim_threshold,
        keyword_threshold=keyword_threshold,
        window_hours=window_hours,
        high_band_kw_floor=high_band_kw_floor,
        llm=llm,
    )

    db.commit()
    return {
        "clusters": len(clusters),
        "events_updated": updated,
        "events_created": created,
        "events_merged": merged,
    }


def recompute_event_scores(db: Session) -> None:
    """事件热度重算（2026-10-12 改版，AIHOT「按事件算热度」语义）。

    口径由 event_hot_mode 切换（管理面板热调，可回滚）：
    - distinct_source（默认）：每独立来源计成员最高一篇再求和——一家媒体发
      十篇只算一次，重复抓取不重复计分，榜单反映「有多少家在说」；
    - sum_articles：旧口径（成员分求和）。
    两口径同为单条 SQL 聚合下推 DB（复用 hot_score_expr），替代逐事件逐成员
    的 N+1 查询；无成员的事件记 0（与旧实现一致）。成员聚合限
    EVENT_SCORE_WINDOW_HOURS 窗口内发布的文章（2026-10-14 改版，对标 AIHOT
    48h 硬窗口）：窗口外成员不再计入，老事件随窗口滑出自然归零沉底。

    上升标记：hot_score_prev 为滚动比较基准——距上次刷新超过滚动窗（6h，
    等效 AIHOT「和 6 小时前比」）时翻新为上一窗口末分数，之后各轮保持；
    is_rising = 新分较基准涨幅超 event_rising_threshold。纯规则见
    hot_score.event_rising_state（有单测），此处只做列写入。

    load_only 只取重算涉及的列：事件行携带 384 维 centroid 与
    timeline/impacts JSONB 大字段，全表每 10 分钟拖一遍纯属浪费。
    """
    from sqlalchemy import func
    from sqlalchemy.orm import load_only

    from app.algorithms.hot_score import event_rising_state, hot_score_expr
    from app.core.config import get_settings
    from app.utils.tz import bj_now

    s = get_settings()
    now = bj_now()
    window_start = now - timedelta(hours=EVENT_SCORE_WINDOW_HOURS)
    if (s.event_hot_mode or "distinct_source") == "sum_articles":
        rows = (
            db.query(
                EventArticle.event_id,
                func.sum(hot_score_expr(Article)),
            )
            .join(Article, Article.id == EventArticle.article_id)
            .filter(Article.publish_time >= window_start)
            .group_by(EventArticle.event_id)
            .all()
        )
        scores = {eid: round(float(total or 0.0), 2) for eid, total in rows}
    else:
        sub = (
            db.query(
                EventArticle.event_id.label("event_id"),
                Article.source.label("source"),
                func.max(hot_score_expr(Article)).label("src_score"),
            )
            .join(Article, Article.id == EventArticle.article_id)
            .filter(Article.publish_time >= window_start)
            .group_by(EventArticle.event_id, Article.source)
            .subquery()
        )
        rows = (
            db.query(sub.c.event_id, func.sum(sub.c.src_score))
            .group_by(sub.c.event_id)
            .all()
        )
        scores = {eid: round(float(total or 0.0), 2) for eid, total in rows}

    events = (
        db.query(HotEvent)
        .options(
            load_only(
                HotEvent.id,
                HotEvent.hot_score,
                HotEvent.hot_score_prev,
                HotEvent.score_updated_at,
                HotEvent.last_time,
                HotEvent.status,
            )
        )
        .all()
    )
    for ev in events:
        old = float(ev.hot_score or 0.0)
        new = scores.get(ev.id, 0.0)
        prev, rising = event_rising_state(
            ev.hot_score_prev,
            ev.score_updated_at,
            old,
            new,
            now,
            threshold=s.event_rising_threshold,
        )
        ev.hot_score_prev = prev
        ev.is_rising = rising
        ev.hot_score = new
        ev.score_updated_at = now
        if ev.last_time:
            # 状态全量刷新：事件状态此前只在被聚类命中时更新，长时间无新报道的
            # 事件会永久冻结在「发酵中/持续关注」——每轮随热度重算一起翻牌
            ev.status = _event_status(ev.last_time)
    db.commit()
