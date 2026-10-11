"""对外内容出口（公开只读）：RSS 订阅 / llms.txt / sitemap / 只读 OpenAPI。

全站门禁的第二块例外域（第一块是 public.py 健康探针）：本模块端点不套
require_user，但受 public_exports_enabled 开关总控——关闭（默认）时一律 404
（不暴露存在性，管理面板可热调）。

版权红线：出口只含本系统自有内容（标题 / 原文链接 / AI 摘要 / 标签 / 事件综述），
绝不输出 Article.content——第三方正文不对外再分发。

性能与礼貌：响应带 ETag（内容哈希，命中 If-None-Match 回 304）与
Cache-Control: public, max-age=600；nginx 侧对 feed/agent 路径用独立的
低速率限流桶（见 frontend/nginx.conf feed_limit），订阅器/Agent 轮询不占用
业务 API 的 10r/s 配额。查询均为单条语句 + selectinload，无逐行懒加载。
"""
import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.algorithms.hot_score import hot_score_expr
from app.api.v2.routes import _has_report_content  # 早报"有实质内容"判定同口径复用
from app.core.config import get_settings
from app.core.db import get_db
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.services import config_service
from app.services.feed_builder import RSS_CONTENT_TYPE, channel as rss_channel, item as rss_item
from app.utils.tz import TZ_BJ

router = APIRouter(prefix="/api/v2")

_TRUE = {"1", "true", "yes", "on"}
_CACHE_SECONDS = 600  # 订阅器合理轮询节奏（秒），与 <ttl>10</ttl> 一致


def _enabled(db: Session) -> bool:
    """public_exports_enabled 生效值（DB 覆盖 .env，面板热调）。"""
    try:
        raw = config_service.get_effective(db, "public_exports_enabled")
    except Exception:  # noqa: BLE001  配置读取失败按默认关闭处理（安全侧）
        raw = get_settings().public_exports_enabled
    return str(raw).strip().lower() in _TRUE


def _gate(db: Session) -> None:
    """开关关闭时 404（而非 401/403：不向匿名探测暴露端点存在性）。"""
    if not _enabled(db):
        raise HTTPException(404, "not found")


def _base_url(request: Request) -> str:
    """feed 内绝对链接的基准：优先显式配置（生产反代/HTTPS 建议设置），
    留空用请求 Host 推导。"""
    configured = (get_settings().public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _feed_limit(db: Session) -> int:
    try:
        value = int(config_service.get_effective(db, "feed_max_items"))
    except Exception:  # noqa: BLE001
        value = get_settings().feed_max_items
    return max(1, min(200, value))


def _xml_response(request: Request, xml: str) -> Response:
    etag = '"' + hashlib.sha256(xml.encode("utf-8")).hexdigest()[:24] + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=" + str(_CACHE_SECONDS)}
    inm = request.headers.get("if-none-match", "")
    if inm.strip() == "*" or etag in [t.strip() for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(content=xml, media_type=RSS_CONTENT_TYPE, headers=headers)


def _article_items(articles: list[Article]) -> list[str]:
    """Article(+Summary) → RSS <item> 列表。description 取 AI 一句话摘要，
    无摘要时退化为「来源：标题」，绝不携带正文。"""
    items: list[str] = []
    for a in articles:
        s = a.summary
        items.append(
            rss_item(
                title=a.title,
                link=a.url,
                description=(s.summary_short if s else "") or (a.source + "报道：" + a.title),
                pub_date=a.publish_time,
                categories=(s.tags or []) if s else (),
            )
        )
    return items


def _event_note(summary: str | None, n_articles: int, n_sources: int) -> str:
    """事件综述 → feed 描述。事件 summary 常为「围绕「…」共有 N 篇…」模板句
    （标题复读、无事实增量），退化为结构化计数（口径同 report_push 的降级）。"""
    text = (summary or "").strip()
    if text.startswith("围绕「"):
        parts = ["聚合 " + str(n_articles) + " 篇报道"]
        if n_sources > 1:
            parts.append(str(n_sources) + " 家来源交叉报道")
        return "，".join(parts) + "。"
    return text or ("聚合 " + str(n_articles) + " 篇报道。")


def _event_members_payload(db: Session, ids: list[int]):
    """批量取事件聚合统计（条数/独立来源数）与最新成员链接（作 item link）。"""
    stats: dict[int, tuple[int, int]] = {}
    anchor: dict[int, str] = {}
    if not ids:
        return stats, anchor
    for eid, n, src_n in (
        db.query(
            EventArticle.event_id,
            func.count(EventArticle.article_id),
            func.count(func.distinct(Article.source)),
        )
        .join(Article, Article.id == EventArticle.article_id)
        .filter(EventArticle.event_id.in_(ids))
        .group_by(EventArticle.event_id)
        .all()
    ):
        stats[eid] = (int(n), int(src_n))
    members = (
        db.query(EventArticle.event_id, Article.url)
        .join(Article, Article.id == EventArticle.article_id)
        .filter(EventArticle.event_id.in_(ids))
        .order_by(Article.publish_time.desc(), Article.id.desc())
        .all()
    )
    for eid, url in members:
        anchor.setdefault(eid, url)
    return stats, anchor


# ———————————————————— RSS 订阅 ————————————————————


@router.get("/feed/latest.xml")
def feed_latest(request: Request, db: Session = Depends(get_db)):
    """最新资讯：按发布时间倒序的全部资讯（AI 摘要版）。"""
    _gate(db)
    articles = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .order_by(Article.publish_time.desc(), Article.id.desc())
        .limit(_feed_limit(db))
        .all()
    )
    base = _base_url(request)
    xml = rss_channel(
        title="华军软件园资讯雷达 · 最新资讯",
        link=base,
        description="最新科技资讯（AI 摘要版）：标题与摘要为本系统生成，正文请点击原文链接",
        items=_article_items(articles),
        self_url=base + "/api/v2/feed/latest.xml",
    )
    return _xml_response(request, xml)


@router.get("/feed/hot.xml")
def feed_hot(request: Request, db: Session = Depends(get_db)):
    """24 小时热榜：热度算法排序（口径与 /hot/board 一致，全局归一化基准）。"""
    _gate(db)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    articles = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .filter(Article.publish_time >= since)
        .order_by(
            hot_score_expr(Article).desc(),
            Article.publish_time.desc(),
            Article.id.desc(),
        )
        .limit(_feed_limit(db))
        .all()
    )
    base = _base_url(request)
    xml = rss_channel(
        title="华军软件园资讯雷达 · 24 小时热榜",
        link=base,
        description="近 24 小时按热度排序的科技资讯（AI 摘要版）",
        items=_article_items(articles),
        self_url=base + "/api/v2/feed/hot.xml",
    )
    return _xml_response(request, xml)


@router.get("/feed/events.xml")
def feed_events(request: Request, db: Session = Depends(get_db)):
    """热点事件：多来源聚合事件（含事件综述），link 指向最新成员报道原文。"""
    _gate(db)
    events = (
        db.query(HotEvent)
        .order_by(HotEvent.hot_score.desc(), HotEvent.id.desc())
        .limit(_feed_limit(db))
        .all()
    )
    stats, anchor = _event_members_payload(db, [e.id for e in events])
    base = _base_url(request)
    items = [
        rss_item(
            title=e.title,
            link=anchor.get(e.id) or base,
            description=_event_note(e.summary, *stats.get(e.id, (0, 0))),
            pub_date=e.last_time or e.created_at or datetime.now(timezone.utc),
            categories=(e.status,),
        )
        for e in events
    ]
    xml = rss_channel(
        title="华军软件园资讯雷达 · 热点事件",
        link=base,
        description="多来源报道聚合的热点事件（含事件综述与来源聚合计数）",
        items=items,
        self_url=base + "/api/v2/feed/events.xml",
    )
    return _xml_response(request, xml)


@router.get("/feed/report.xml")
def feed_report(request: Request, db: Session = Depends(get_db)):
    """今日早报：每期一条 item（TOP 事件 + 热点速览的规则汇编，不含第三方正文）。"""
    _gate(db)
    rows = db.query(DailyReport).order_by(DailyReport.date.desc()).limit(30).all()
    rows = [r for r in rows if _has_report_content(r.content)][:10]
    base = _base_url(request)
    items = []
    for r in rows:
        c = r.content or {}
        lines = [
            "· " + str(e.get("title", "")) + "（" + str(e.get("articles_count", 0)) + "篇/" + str(e.get("sources_count", 0)) + "源）"
            for e in (c.get("top_events") or [])[:5]
        ]
        quick = "；".join(str(a.get("title", "")) for a in (c.get("quick_news") or [])[:8])
        if quick:
            lines.append("热点速览：" + quick)
        items.append(
            rss_item(
                title="今日早报 第" + str(r.issue_no) + "期（" + r.date.isoformat() + "）",
                link=base,
                description="\n".join(lines) or "（本期无实质内容）",
                # 早报生成时刻为每日 08:00 北京时间，作为 pubDate 语义最稳
                pub_date=datetime(r.date.year, r.date.month, r.date.day, 8, 0, tzinfo=TZ_BJ),
            )
        )
    xml = rss_channel(
        title="华军软件园资讯雷达 · 今日早报",
        link=base,
        description="每日 08:00（北京时间）自动生成的热点事件早报",
        items=items,
        self_url=base + "/api/v2/feed/report.xml",
    )
    return _xml_response(request, xml)


@router.get("/feed/channel/{channel_name}.xml")
def feed_channel(channel_name: str, request: Request, db: Session = Depends(get_db)):
    """频道订阅：未知频道 404（订阅器可感知配置错误），已知频道零资讯返回空刊。"""
    _gate(db)
    known = {r[0] for r in db.query(Article.channel).distinct().all()}
    if channel_name not in known:
        raise HTTPException(404, "unknown channel")
    articles = (
        db.query(Article)
        .options(selectinload(Article.summary))
        .filter(Article.channel == channel_name)
        .order_by(Article.publish_time.desc(), Article.id.desc())
        .limit(_feed_limit(db))
        .all()
    )
    base = _base_url(request)
    xml = rss_channel(
        title="华军软件园资讯雷达 · " + channel_name,
        link=base,
        description=channel_name + " 频道最新资讯（AI 摘要版）",
        items=_article_items(articles),
        self_url=base + "/api/v2/feed/channel/" + channel_name + ".xml",
    )
    return _xml_response(request, xml)


# ———————————————————— Agent 入口 ————————————————————


@router.get("/agent/llms.txt")
def agent_llms(request: Request, db: Session = Depends(get_db)):
    """llms.txt（llmstxt.org 约定）：面向 AI Agent 的数据入口说明。"""
    _gate(db)
    base = _base_url(request)
    s = get_settings()
    try:
        interval = int(config_service.get_effective(db, "crawler_interval_minutes"))
    except Exception:  # noqa: BLE001
        interval = s.crawler_interval_minutes
    channels = sorted(r[0] for r in db.query(Article.channel).distinct().all())
    ch_lines = "\n".join(
        "- [频道 " + c + "](" + base + "/api/v2/feed/channel/" + c + ".xml): 该频道最新资讯"
        for c in channels
    )
    try:
        mcp_token_set = bool(config_service.get_effective(db, "mcp_token"))
    except Exception:  # noqa: BLE001
        mcp_token_set = bool(s.mcp_token)
    mcp_line = (
        "[MCP 服务器](" + base + "/api/v2/mcp): 需 Bearer 令牌（向管理员获取）"
        if mcp_token_set
        else "MCP 服务器: 未开放"
    )
    content = (
        "# 华军软件园资讯雷达\n\n"
        "> 多源科技资讯聚合与热点分析系统：AI 摘要、热点事件聚类、每日早报。"
        "本页面向 AI Agent 与订阅器说明可读取的数据入口。\n\n"
        "## 订阅（RSS 2.0，每条含标题 / 原文链接 / AI 摘要）\n\n"
        "- [最新资讯](" + base + "/api/v2/feed/latest.xml): 按发布时间倒序的全部资讯\n"
        "- [24 小时热榜](" + base + "/api/v2/feed/hot.xml): 按热度算法排序的近 24h 资讯\n"
        "- [热点事件](" + base + "/api/v2/feed/events.xml): 多来源聚合的热点事件（含事件综述）\n"
        "- [今日早报](" + base + "/api/v2/feed/report.xml): 每日 08:00（北京时间）自动生成的事件早报\n"
        + (ch_lines + "\n" if ch_lines else "") +
        "\n## 数据接口（只读）\n\n"
        "- [站点地图](" + base + "/api/v2/agent/sitemap.xml): 可订阅入口清单\n"
        "- [只读 OpenAPI](" + base + "/api/v2/agent/openapi.json): 公开端点的机器描述\n"
        "- " + mcp_line + "\n\n"
        "## 数据说明\n\n"
        "- 本系统输出的是自有生成内容（AI 摘要 / 事件综述 / 元数据）；文章正文版权归各原始来源，请通过链接访问原文。\n"
        "- 数据节奏：采集约每 " + str(interval) + " 分钟一轮，互动数据每 15 分钟轮询，事件聚类每 10 分钟，早报每日 08:00（北京时间）。\n"
        "- 反馈与授权事宜请联系站点管理员。\n"
    )
    return Response(content=content, media_type="text/plain; charset=utf-8")


@router.get("/agent/sitemap.xml")
def agent_sitemap(request: Request, db: Session = Depends(get_db)):
    """站点地图：站点主体需登录，此处仅列出公开只读出口，供搜索引擎/Agent 发现。"""
    _gate(db)
    base = _base_url(request)
    paths = [
        "/api/v2/feed/latest.xml",
        "/api/v2/feed/hot.xml",
        "/api/v2/feed/events.xml",
        "/api/v2/feed/report.xml",
        "/api/v2/agent/llms.txt",
        "/api/v2/agent/openapi.json",
    ]
    body = "".join("<url><loc>" + base + p + "</loc></url>" for p in paths)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + body + "</urlset>"
    )
    return Response(content=xml, media_type="application/xml; charset=utf-8")


@router.get("/agent/openapi.json")
def agent_openapi(request: Request, db: Session = Depends(get_db)):
    """只读 OpenAPI：手工维护的公开端点子集（主 openapi 受 ENABLE_DOCS 门控，
    且包含管理面，不适合对外发布）。"""
    _gate(db)
    base = _base_url(request)

    def _get(summary: str) -> dict:
        return {
            "get": {
                "summary": summary,
                "responses": {
                    "200": {"description": "OK"},
                    "304": {"description": "ETag 命中"},
                    "404": {"description": "出口关闭或资源不存在"},
                },
            }
        }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "华军软件园资讯雷达 · 公开只读接口",
            "version": get_settings().version,
            "description": "仅含公开只读出口；站点业务与管理接口需登录，不在此列。",
        },
        "servers": [{"url": base}],
        "paths": {
            "/api/v2/feed/latest.xml": _get("最新资讯 RSS"),
            "/api/v2/feed/hot.xml": _get("24 小时热榜 RSS"),
            "/api/v2/feed/events.xml": _get("热点事件 RSS"),
            "/api/v2/feed/report.xml": _get("今日早报 RSS"),
            "/api/v2/feed/channel/{channel_name}.xml": _get("频道 RSS"),
            "/api/v2/agent/llms.txt": _get("Agent 入口说明"),
            "/api/v2/agent/sitemap.xml": _get("公开出口站点地图"),
        },
    }
