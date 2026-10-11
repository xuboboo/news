"""对外内容出口（exports 域）测试：开关门禁 / RSS 内容与版权红线 / ETag / 频道与事件 / llms.txt / MCP 门禁。"""
import re
from datetime import date, datetime, timezone

import pytest

from conftest import set_config

from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport
from app.models.summary import ArticleSummary

# 第三方正文标记：任何 feed/agent 输出中出现即视为版权红线被击穿
SECRET = "THIRD-PARTY-CONTENT-MUST-NOT-LEAK"

_EXPORT_PATHS = (
    "/api/v2/feed/latest.xml",
    "/api/v2/feed/hot.xml",
    "/api/v2/feed/events.xml",
    "/api/v2/feed/report.xml",
    "/api/v2/agent/llms.txt",
    "/api/v2/agent/sitemap.xml",
    "/api/v2/agent/openapi.json",
)

# RFC-822：Sat, 10 Oct 2026 12:00:00 +0000
_PUBDATE_RE = re.compile(r"<pubDate>[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}</pubDate>")


def _mk_article(db, url="https://example.com/a/1", channel="软件之家", title="测试文章标题"):
    s = db.factory()
    a = Article(
        title=title,
        url=url,
        channel=channel,
        source="IT之家",
        content="正文素材。" + SECRET,
        publish_time=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        ai_status="summarized",
    )
    s.add(a)
    s.flush()
    s.add(
        ArticleSummary(
            article_id=a.id,
            summary_short="一句话摘要",
            summary_long="",
            key_points=[],
            tags=["AI", "测试"],
            model="test",
        )
    )
    s.commit()
    return a.id


def test_exports_disabled_returns_404(client):
    """总开关默认关闭：全部出口端点 404（不暴露存在性），不 401/403。"""
    for p in _EXPORT_PATHS:
        r = client.get(p)
        assert r.status_code == 404, p


def test_latest_feed_enabled(client, db):
    _mk_article(db)
    set_config(db, "public_exports_enabled", "true")
    r = client.get("/api/v2/feed/latest.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    assert "测试文章标题" in r.text
    assert "一句话摘要" in r.text
    assert "https://example.com/a/1" in r.text
    # 版权红线：第三方正文绝不出现
    assert SECRET not in r.text
    assert _PUBDATE_RE.search(r.text)
    assert 'xmlns:atom="http://www.w3.org/2005/Atom"' in r.text


def test_feed_etag_304(client, db):
    _mk_article(db)
    set_config(db, "public_exports_enabled", "true")
    r1 = client.get("/api/v2/feed/latest.xml")
    etag = r1.headers["etag"]
    r2 = client.get("/api/v2/feed/latest.xml", headers={"if-none-match": etag})
    assert r2.status_code == 304
    assert r2.text == ""
    # 内容变化后 ETag 失效
    _mk_article(db, url="https://example.com/a/2", title="第二篇标题")
    r3 = client.get("/api/v2/feed/latest.xml", headers={"if-none-match": etag})
    assert r3.status_code == 200


def test_unknown_channel_404(client, db):
    set_config(db, "public_exports_enabled", "true")
    assert client.get("/api/v2/feed/channel/不存在频道.xml").status_code == 404


def test_channel_feed(client, db):
    _mk_article(db, channel="软件之家")
    set_config(db, "public_exports_enabled", "true")
    r = client.get("/api/v2/feed/channel/软件之家.xml")
    assert r.status_code == 200
    assert "测试文章标题" in r.text
    assert SECRET not in r.text


def test_events_feed_template_summary_degrades(client, db):
    """事件综述为模板句时退化为结构化计数（同 report_push 降级口径），link 指向最新成员原文。"""
    aid = _mk_article(db)
    s = db.factory()
    ev = HotEvent(
        title="热点事件E",
        summary="围绕「热点事件E」共有 2 篇相关报道：",
        hot_score=9.0,
        first_time=datetime(2026, 10, 10, tzinfo=timezone.utc),
        last_time=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        status="发酵中",
        impacts=[],
        timeline=[],
    )
    s.add(ev)
    s.flush()
    s.add(EventArticle(event_id=ev.id, article_id=aid))
    s.commit()
    set_config(db, "public_exports_enabled", "true")
    r = client.get("/api/v2/feed/events.xml")
    assert r.status_code == 200
    assert "聚合 1 篇报道" in r.text
    assert "https://example.com/a/1" in r.text


def test_report_feed(client, db):
    s = db.factory()
    s.add(
        DailyReport(
            date=date(2026, 10, 10),
            issue_no=5,
            content={
                "top_events": [
                    {"title": "事件甲", "summary": "s", "articles_count": 3, "sources_count": 2}
                ],
                "quick_news": [{"title": "快讯一"}],
            },
        )
    )
    s.commit()
    set_config(db, "public_exports_enabled", "true")
    r = client.get("/api/v2/feed/report.xml")
    assert r.status_code == 200
    assert "今日早报 第5期（2026-10-10）" in r.text
    assert "事件甲" in r.text
    assert "快讯一" in r.text


def test_llms_sitemap_openapi(client, db):
    _mk_article(db)
    set_config(db, "public_exports_enabled", "true")
    r = client.get("/api/v2/agent/llms.txt")
    assert r.status_code == 200
    assert "/api/v2/feed/latest.xml" in r.text
    assert "版权归各原始来源" in r.text
    sm = client.get("/api/v2/agent/sitemap.xml")
    assert sm.status_code == 200
    assert "<urlset" in sm.text
    op = client.get("/api/v2/agent/openapi.json")
    assert op.status_code == 200
    assert op.json()["paths"]["/api/v2/feed/latest.xml"]


def test_llms_lists_channels(client, db):
    _mk_article(db, channel="软件之家")
    set_config(db, "public_exports_enabled", "true")
    r = client.get("/api/v2/agent/llms.txt")
    assert "/api/v2/feed/channel/软件之家.xml" in r.text


def test_mcp_gate_logic(db, monkeypatch):
    """MCP 门禁逻辑：开关与 Bearer 任一不满足即拒绝；齐备才放行。

    说明：不走到 SDK 传输层——StreamableHTTPSessionManager 需要 lifespan
    （uvicorn 正常运行时生效），离线测试直接验证自研 _BearerGate 的判定与
    主应用挂载存在性；协议层行为由 mcp SDK 自身测试覆盖。
    """
    pytest.importorskip("mcp")
    from app.services import mcp_server

    monkeypatch.setattr(mcp_server, "_session_factory", lambda: db.factory)
    gate = mcp_server._BearerGate(None)
    authed = {"type": "http", "headers": [(b"authorization", b"Bearer tok-123")]}
    # token 未配置：恒拒绝
    assert gate._check(authed) is False
    set_config(db, "public_exports_enabled", "true")
    set_config(db, "mcp_token", "tok-123")
    assert gate._check(authed) is True
    # 无 Bearer / 错 Bearer：拒绝
    assert gate._check({"type": "http", "headers": []}) is False
    assert gate._check({"type": "http", "headers": [(b"authorization", b"Bearer wrong")]}) is False
    # 非 http scope（无 authorization 头）：_check 恒 False（__call__ 对非 http scope
    # 不走 _check 直接透传内层，这里只锁定头的读取路径）
    assert gate._check({"type": "lifespan"}) is False
    # 总开关关闭：token 正确也拒绝
    set_config(db, "public_exports_enabled", "false")
    assert gate._check(authed) is False


def test_mcp_mounted():
    """主应用确实挂载了 MCP 子应用（SDK 可用为前提；不可用时挂载缺失属正常形态）。"""
    pytest.importorskip("mcp")
    from app import main as app_main

    mounts = [
        getattr(r, "path", "")
        for r in app_main.app.router.routes
        if type(r).__name__ == "Mount"
    ]
    assert "/api/v2/mcp" in mounts
