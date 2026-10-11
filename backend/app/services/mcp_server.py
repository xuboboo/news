"""MCP 只读服务器（exports 域可选能力；依赖 mcp SDK，缺失时自动不挂载）。

- 挂载：/api/v2/mcp（streamable-HTTP，无状态），由 main.py 启动时调用 mount_mcp
- 门禁：public_exports_enabled 总开关 + Bearer mcp_token（均面板热调）；
  任一不满足返回 404（不暴露存在性，与 exports._gate 同一策略）
- 只读：tools 仅打开只读会话，不提供任何写操作；素材口径与 exports.py 一致
  （标题/链接/AI 摘要/事件综述，不含第三方正文）
"""
import contextlib
import hmac
import logging

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

MCP_MOUNT_PATH = "/api/v2/mcp"


def _session_factory():
    """会话工厂（函数内 import：测试可 monkeypatch 本函数注入 SQLite 会话）。"""
    from app.core.db import SessionLocal

    return SessionLocal


def _db_state() -> tuple[bool, str]:
    """门禁 DB 态：(总开关是否打开, 生效 token)。"""
    from app.services import config_service

    with _session_factory()() as db:
        raw = config_service.get_effective(db, "public_exports_enabled")
        enabled = str(raw).strip().lower() in {"1", "true", "yes", "on"}
        token = str(config_service.get_effective(db, "mcp_token") or "")
    return enabled, token


class _BearerGate:
    """ASGI 中间件：Bearer mcp_token + 总开关校验，不通过一律 404。

    DB 校验放线程池，不阻塞事件循环（MCP 低频调用，开销可忽略）。
    """

    def __init__(self, inner):
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self._inner(scope, receive, send)
        ok = await run_in_threadpool(self._check, scope)
        if not ok:
            from fastapi.responses import JSONResponse

            response = JSONResponse({"detail": "not found"}, status_code=404)
            return await response(scope, receive, send)
        await self._inner(scope, receive, send)

    def _check(self, scope) -> bool:
        try:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in (scope.get("headers") or [])
            }
        except Exception:  # noqa: BLE001
            return False
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        enabled, token = _db_state()
        if not enabled or not token:
            return False
        return hmac.compare_digest(auth[len("Bearer "):].strip(), token)


def mount_mcp(app) -> bool:
    """挂载 MCP ASGI 子应用；SDK 缺失/版本不兼容返回 False（主应用照常启动）。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        logger.info("mcp SDK 未安装，MCP 服务器不挂载（pip install mcp 后重启生效）")
        return False

    try:
        try:
            # streamable_http_path="/"：让传输层直接服务挂载点根路径
            # （默认 "/mcp" 会出现 /api/v2/mcp/mcp 这类嵌套 URL）
            mcp = FastMCP("hjradar", stateless_http=True, json_response=True, streamable_http_path="/")
        except TypeError:
            # 旧版 SDK 不认较新的参数：退回最小构造（传输路径随 SDK 默认值）
            mcp = FastMCP("hjradar", stateless_http=True, json_response=True)

        @mcp.tool()
        def search_articles(keyword: str, limit: int = 10) -> list[dict]:
            """按关键词检索资讯（标题/标签模糊匹配），返回标题、原文链接、来源、发布时间与 AI 摘要。"""
            from sqlalchemy import String, cast, or_
            from sqlalchemy.orm import selectinload

            from app.api.v2.routes import _escape_like
            from app.models.article import Article
            from app.models.summary import ArticleSummary

            with _session_factory()() as db:
                like = "%" + _escape_like(keyword) + "%"
                rows = (
                    db.query(Article)
                    .options(selectinload(Article.summary))
                    .outerjoin(ArticleSummary, ArticleSummary.article_id == Article.id)
                    .filter(
                        or_(
                            Article.title.ilike(like, escape="\\"),
                            cast(ArticleSummary.tags, String).ilike(like, escape="\\"),
                        )
                    )
                    .order_by(Article.publish_time.desc(), Article.id.desc())
                    .limit(max(1, min(50, int(limit))))
                    .all()
                )
                return [
                    {
                        "title": a.title,
                        "url": a.url,
                        "source": a.source,
                        "channel": a.channel,
                        "publish_time": a.publish_time.isoformat() if a.publish_time else None,
                        "summary_short": a.summary.summary_short if a.summary else "",
                    }
                    for a in rows
                ]

        @mcp.tool()
        def get_hot_events(limit: int = 10) -> list[dict]:
            """当前热点事件（按事件热度降序），含事件综述、状态与来源聚合计数。"""
            from sqlalchemy import func

            from app.models.article import Article
            from app.models.event import EventArticle, HotEvent

            with _session_factory()() as db:
                events = (
                    db.query(HotEvent)
                    .order_by(HotEvent.hot_score.desc(), HotEvent.id.desc())
                    .limit(max(1, min(50, int(limit))))
                    .all()
                )
                out = []
                for e in events:
                    n, src = (
                        db.query(
                            func.count(EventArticle.article_id),
                            func.count(func.distinct(Article.source)),
                        )
                        .join(Article, Article.id == EventArticle.article_id)
                        .filter(EventArticle.event_id == e.id)
                        .one()
                    )
                    out.append(
                        {
                            "id": e.id,
                            "title": e.title,
                            "summary": e.summary,
                            "status": e.status,
                            "hot_score": e.hot_score,
                            "articles_count": int(n),
                            "sources_count": int(src),
                            "last_time": e.last_time.isoformat() if e.last_time else None,
                        }
                    )
                return out

        @mcp.tool()
        def get_event(event_id: int) -> dict:
            """获取热点事件详情：综述、状态、时间线与成员报道（标题/原文链接/来源）。"""
            from app.models.article import Article
            from app.models.event import EventArticle, HotEvent

            with _session_factory()() as db:
                ev = db.get(HotEvent, int(event_id))
                if ev is None:
                    return {"error": "event not found"}
                members = (
                    db.query(Article)
                    .join(EventArticle, EventArticle.article_id == Article.id)
                    .filter(EventArticle.event_id == ev.id)
                    .order_by(Article.publish_time.desc(), Article.id.desc())
                    .all()
                )
                return {
                    "id": ev.id,
                    "title": ev.title,
                    "summary": ev.summary,
                    "status": ev.status,
                    "hot_score": ev.hot_score,
                    "timeline": ev.timeline or [],
                    "members": [
                        {
                            "title": a.title,
                            "url": a.url,
                            "source": a.source,
                            "publish_time": a.publish_time.isoformat() if a.publish_time else None,
                        }
                        for a in members
                    ],
                }

        @mcp.tool()
        def get_daily_report(date: str = "") -> dict:
            """获取早报内容（YYYY-MM-DD）；留空取最近一期有实质内容的早报。"""
            from datetime import date as date_type

            from app.models.report import DailyReport

            with _session_factory()() as db:
                if date:
                    row = db.query(DailyReport).filter_by(date=date_type.fromisoformat(date)).first()
                    if row is None:
                        return {"error": "report not found"}
                else:
                    rows = db.query(DailyReport).order_by(DailyReport.date.desc()).limit(7).all()
                    row = next(
                        (
                            r
                            for r in rows
                            if (r.content or {}).get("top_events") or (r.content or {}).get("quick_news")
                        ),
                        None,
                    )
                    if row is None:
                        return {"error": "no report yet"}
                return {
                    "date": row.date.isoformat(),
                    "issue_no": row.issue_no,
                    "content": row.content or {},
                }

        asgi = mcp.streamable_http_app()
    except Exception:  # noqa: BLE001  SDK 版本漂移：宁可降级不挂载，不可拖垮主应用
        logger.exception("MCP 初始化失败，不挂载")
        return False

    # Starlette 挂载子应用不会传递 lifespan：StreamableHTTPSessionManager 的
    # task group 必须显式 run()，链入主应用 lifespan（启动即开，关停即收）
    session_manager = mcp.session_manager
    _orig_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan_with_mcp(a):
        async with session_manager.run():
            async with _orig_lifespan(a):
                yield

    app.router.lifespan_context = _lifespan_with_mcp
    app.mount(MCP_MOUNT_PATH, _BearerGate(asgi))
    logger.info("MCP 服务器已挂载 %s（Bearer 门禁 + exports 总开关）", MCP_MOUNT_PATH)
    return True
