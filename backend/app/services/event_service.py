"""事件服务：Embedding 聚类 → event / event_article 落库（PRD 第 5/9/10 节）。

增量逻辑：已有事件以质心参与聚类，新成员命中则并入；全新簇创建新事件。
事件总结与核心影响优先由 LLM 生成（配置后自动启用），否则由成员报道事实拼装。
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.ai.embedding import cosine
from app.ai.event_cluster import ClusterItem, cluster_articles, event_time_range, event_title_for
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.services import config_service
from app.services.usage_service import extract_openai_usage, record_usage
from app.utils.text import keyword_jaccard, truncate, truncate_boundary
from app.utils.url_guard import assert_public_http_url


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# 仅识别"致谢行"类无效摘要（窄口径：正常导语如「IT之家 9 月 6 日消息」不算脏数据）
_DIRTY_SUMMARY = re.compile(r"线索投递|^(感谢|题图|配图|图片来源)")


def _summary_dirty(text: str | None) -> bool:
    return bool(text) and bool(_DIRTY_SUMMARY.search(text.strip()))


def _event_status(last_time: datetime) -> str:
    hours = (datetime.now(timezone.utc) - _tz(last_time)).total_seconds() / 3600
    if hours <= 6:
        return "发酵中"
    if hours <= 24:
        return "持续关注"
    return "已平息"


def _compose_summary(members: list[Article]) -> str:
    """无 LLM 时的事件总结：纯事实拼装（来源 + 报道要点），不添加观点。"""
    members = [m for m in members if m]
    if len(members) == 1:
        a = members[0]
        if a.summary and a.summary.summary_short and not _summary_dirty(a.summary.summary_short):
            return a.summary.summary_short
        return a.title
    lines = []
    for a in members[:4]:
        lines.append(f"{a.source}报道：{truncate(a.title, 48)}")
    head = f"围绕「{truncate(members[0].title, 24)}」共有 {len(members)} 篇来自不同报道的关联文章："
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
    llm: tuple[str, str, str] | None = None,
    event_id: int | None = None,
) -> tuple[str, list[str]] | None:
    """LLM 生成事件总结 + 核心影响；未配置或失败返回 None（失败已落账）。

    用量记账（task_type=event_summary）：成功记 success；HTTP 200 但解析失败记
    parse_fail（token 已花）；连接/非 200 记 api_fail。未配置 LLM 时不落账
    （降级拼装 _compose_summary 无 API 调用）。event_id 为空时（新建事件、
    尚未 flush）流水只带标题上下文。
    """
    if not llm or not (llm[0] and llm[1] and llm[2]):
        return None
    base_url, api_key, model = llm
    start = time.monotonic()
    http_ok = False  # 是否已收到 HTTP 200（区分 api_fail / parse_fail）
    usage = {}
    try:
        base_url = assert_public_http_url(base_url)  # 违规 → api_fail 落账后降级事实拼装
        material = "\n".join(
            f"- [{a.source}] {a.title}\n  摘要：{a.summary.summary_short if a.summary else ''}"
            for a in members[:6]
        )
        prompt = (
            "以下是一个科技热点事件的多篇报道。请输出严格 JSON："
            '{"summary":"事件总结(150字内,客观,只保留事实)","impacts":["核心影响1","核心影响2","核心影响3"]}\n\n'
            f"事件标题：{title}\n报道：\n{material}"
        )
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        http_ok = True
        body = resp.json()
        # 第一件事提取 usage：即使后续解析失败，token 也已消耗
        usage = extract_openai_usage(body)
        raw = body["choices"][0]["message"]["content"]
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        record_usage(
            task_type="event_summary",
            model=model,
            status="success",
            latency_ms=int((time.monotonic() - start) * 1000),
            event_id=event_id,
            **usage,
        )
        return (
            truncate(str(data.get("summary", "")), 300),
            [str(i) for i in (data.get("impacts") or [])][:4],
        )
    except Exception as exc:
        record_usage(
            task_type="event_summary",
            model=model,
            status="parse_fail" if http_ok else "api_fail",
            error=str(exc),
            latency_ms=int((time.monotonic() - start) * 1000),
            event_id=event_id,
            **usage,
        )
        return None


def run_event_clustering(db: Session) -> dict:
    """对近期已向量化文章执行聚类并增量更新事件库。返回统计。"""
    # 阈值/回看窗读运行时生效值（管理面板可热调，DB 覆盖 .env）
    sim_threshold = float(config_service.get_effective(db, "sim_threshold"))
    keyword_threshold = float(config_service.get_effective(db, "keyword_jaccard_threshold"))
    lookback_hours = int(config_service.get_effective(db, "cluster_lookback_hours"))
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
    clusters = cluster_articles(items, sim_threshold=sim_threshold)
    article_map = {a.id: a for a in articles}

    updated = created = 0
    for cluster in clusters:
        member_articles = [article_map[m.id] for m in cluster.members]

        # 1) 尝试并入已有事件：与簇内合并同口径，须同时满足相似度与标题关键词两条件。
        #    此前只看 Embedding 相似度，实际发生过错合并（2026-09-09：快科技两篇不相关
        #    文章 sim=0.8563 擦线过阈值被并入同一事件，标题关键词重合度实为 0.053）
        target_event: HotEvent | None = None
        rep = cluster.members[0]
        for ev in db.query(HotEvent).filter(
            HotEvent.last_time >= since - timedelta(hours=24)
        ).all():
            ev_vec = list(ev.centroid) if ev.centroid is not None else None
            sim = max(
                (cosine(list(m.embedding), ev_vec) for m in cluster.members),
                default=0.0,
            )
            if sim > sim_threshold and keyword_jaccard(rep.title, ev.title or "") >= keyword_threshold:
                target_event = ev
                break

        if target_event is None:
            # 2) 检查是否已有事件覆盖了大部分成员（避免重复建事件）：
            #    多个候选时选成员重叠数最多的事件，而不是任取第一条关联
            member_ids = [m.id for m in cluster.members]
            existing_links = (
                db.query(EventArticle)
                .filter(EventArticle.article_id.in_(member_ids))
                .all()
            )
            if existing_links:
                overlap: dict[int, int] = {}
                for l in existing_links:
                    overlap[l.event_id] = overlap.get(l.event_id, 0) + 1
                best_ev_id = max(overlap, key=lambda eid: overlap[eid])
                target_event = db.get(HotEvent, best_ev_id)

        if target_event is None:
            # 3) 新建事件
            title = event_title_for(cluster.members)
            first_time, last_time = event_time_range(cluster.members)
            llm = _llm_event_summary(title, member_articles)
            summary_text = llm[0] if llm else _compose_summary(member_articles)
            impacts = llm[1] if llm else _compose_impacts(member_articles)
            target_event = HotEvent(
                title=title,
                summary=summary_text,
                impacts=impacts,
                first_time=first_time,
                last_time=last_time,
                status=_event_status(last_time),
                centroid=cluster.centroid,
                timeline=[],
            )
            db.add(target_event)
            db.flush()
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
            if _tz(target_event.last_time) >= _tz(datetime.now(timezone.utc) - timedelta(hours=24)):
                target_event.centroid = cluster.centroid

        # 4) 维护成员关联与时间线
        member_ids = {m.id for m in cluster.members}
        linked = {
            l.article_id
            for l in db.query(EventArticle).filter_by(event_id=target_event.id).all()
        }
        new_links = member_ids - linked
        for aid in new_links:
            db.add(EventArticle(event_id=target_event.id, article_id=aid))
        if new_links:
            # AI 状态机最后一环：文章已归入事件（pending → summarized → embedded → clustered）
            for m in cluster.members:
                a = article_map.get(m.id)
                if a is not None and a.ai_status != "clustered":
                    a.ai_status = "clustered"

        # 成员变化或存量摘要为套话（致谢行等）时，重算事件总结 / 核心影响 / 时间线，
        # 保证 summary 中的关联文章数始终与 event_article 实际成员数一致
        if new_links or _summary_dirty(target_event.summary):
            all_ids = [
                l.article_id
                for l in db.query(EventArticle).filter_by(event_id=target_event.id).all()
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
                    l.article_id
                    for l in db.query(EventArticle).filter_by(event_id=target_event.id).all()
                ]
                all_members = sorted(
                    (m for m in (article_map.get(a) or db.get(Article, a) for a in all_ids) if m),
                    key=lambda x: x.publish_time,
                )

            if all_members:
                all_members.sort(key=lambda x: x.publish_time)

                llm = _llm_event_summary(
                    target_event.title, all_members, event_id=target_event.id
                )
                target_event.summary = llm[0] if llm else _compose_summary(all_members)
                if llm:
                    target_event.impacts = llm[1]

                timeline = [
                    {
                        "time": _tz(a.publish_time).isoformat(),
                        "text": truncate(a.summary.summary_short, 60) if a.summary else truncate(a.title, 60),
                        "source": a.source,
                    }
                    for a in sorted(all_members, key=lambda x: x.publish_time, reverse=True)
                ]
                target_event.timeline = timeline
                db.flush()
            # all_members 为空（成员全部失效）时仅清理死链并保留原摘要，不再重算

    db.commit()
    return {"clusters": len(clusters), "events_updated": updated, "events_created": created}


def recompute_event_scores(db: Session) -> None:
    """事件热度 = 成员文章 HotScore 之和（归一化在 API 层做）。

    单条 JOIN + GROUP BY 下推 DB（复用 hot_score_expr），替代逐事件逐成员
    的 N+1 查询；无成员的事件记 0（与旧实现一致）。
    """
    from sqlalchemy import func

    from app.algorithms.hot_score import hot_score_expr

    rows = (
        db.query(
            EventArticle.event_id,
            func.sum(hot_score_expr(Article)),
        )
        .join(Article, Article.id == EventArticle.article_id)
        .group_by(EventArticle.event_id)
        .all()
    )
    scores = {eid: round(float(total or 0.0), 2) for eid, total in rows}
    for ev in db.query(HotEvent).all():
        ev.hot_score = scores.get(ev.id, 0.0)
    db.commit()
