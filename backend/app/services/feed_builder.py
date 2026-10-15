"""公开 RSS 2.0 feed 构造（exports 域专用，纯函数、无 DB 依赖）。

口径约束：
- 版权红线：item 只承载本系统自有内容（标题/原文链接/AI 摘要/标签/事件综述），
  description 永远不落 Article.content——第三方正文不对外再分发；
- 时间一律 RFC-822（带数字时区 +0800），naive datetime 按 UTC 解释（库内
  publish_time 均 tz-aware，此分支只为防御）；
- 全文 XML 转义、不用 CDATA（规避 "]]>" 序列与转义双轨维护的心智负担）。
"""
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

RSS_CONTENT_TYPE = "application/rss+xml; charset=utf-8"

# 属性值转义：默认 escape 不处理引号，href 等双引号属性必须补
_ATTR_ENTITIES = {"'": "&apos;", '"': "&quot;"}


def rfc822(dt: datetime) -> str:
    """RFC-822 日期（RSS pubDate 口径）：Sat, 11 Oct 2026 08:00:00 +0800。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def item(
    title: str,
    link: str,
    description: str,
    pub_date: datetime,
    categories=(),
    guid: str | None = None,
) -> str:
    """单条 <item>：guid 缺省取 link（isPermaLink=true），categories 至多 8 个。"""
    is_permalink = guid is None
    g = guid or link
    cats = "".join(
        "<category>" + escape(str(c)) + "</category>" for c in list(categories)[:8] if c
    )
    return (
        "<item>"
        "<title>" + escape(str(title or "")) + "</title>"
        "<link>" + escape(str(link)) + "</link>"
        '<guid isPermaLink="' + ("true" if is_permalink else "false") + '">' + escape(str(g)) + "</guid>"
        "<pubDate>" + rfc822(pub_date) + "</pubDate>"
        + cats +
        "<description>" + escape(str(description or "")) + "</description>"
        "</item>"
    )


def channel(
    title: str,
    link: str,
    description: str,
    items: list[str],
    self_url: str,
    last_build: datetime | None = None,
    ttl_minutes: int = 10,
) -> str:
    """完整 RSS 2.0 文档：atom:link self 供订阅器回链校验，ttl 提示建议轮询间隔。"""
    # The build timestamp is part of the XML and therefore of its ETag. Use a
    # deterministic epoch for empty feeds; callers with data should pass the
    # newest known source/update timestamp.
    lb = last_build or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        "<title>" + escape(title) + "</title>"
        "<link>" + escape(link) + "</link>"
        "<description>" + escape(description) + "</description>"
        "<language>zh-cn</language>"
        "<lastBuildDate>" + rfc822(lb) + "</lastBuildDate>"
        "<ttl>" + str(int(ttl_minutes)) + "</ttl>"
        '<atom:link href="' + escape(self_url, _ATTR_ENTITIES) + '" rel="self" type="application/rss+xml" />'
        "<generator>华军软件园资讯雷达</generator>"
        + "".join(items) +
        "</channel></rss>"
    )
