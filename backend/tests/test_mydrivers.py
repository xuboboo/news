"""快科技爬虫单测：链接提取 / news_id / 面包屑解析 / 时间解析 / 正文清洗 / 文章页解析。

样本取自 2026-09 实抓页面（news.mydrivers.com/1/1149/1149297.htm 等）；
fixtures/kj_article_sample.html 为真实页面结构的最小化样本。
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from app.crawler.mydrivers import (
    _PAGE_TIME_RE,
    _parse_article_html,
    CHANNEL_WHITELIST,
    ENTRY_PAGES,
    clean_content,
    extract_article_links,
    extract_feed_items,
    extract_news_id,
    parse_breadcrumb,
)

# 入口页（technewsall.html）里的协议相对链接形态
ENTRY_HTML = """
<a href="//news.mydrivers.com/1/1149/1149297.htm">8GB内存PC性能实测</a>
<a href="//news.mydrivers.com/1/1149/1149302.htm?from=nav">带参数链接</a>
<a href="//img1.mydrivers.com/img/20260907/x.jpg">图片链接不算</a>
<a href="https://news.mydrivers.com/1/1148/1148111.htm">绝对地址也算</a>
"""

# 文章页面包屑（div.pathway）：站点路径两级 + 一级栏目 + 二级栏目 + 固定推广位
BREADCRUMB_HTML = """
<div class="pathway"><ul><li>当前位置：<a target="_self" href="https://www.mydrivers.com">首页</a>
&gt; <a href="//news.mydrivers.com/">资讯中心</a> &gt;
<a href="//news.mydrivers.com/windows.html">软件之家</a> &gt;
<a href="//news.mydrivers.com/windows.html">桌面系统</a> &gt;
<a href="//news.mydrivers.com/a.html">IT采购专业调查</a></li></ul></div>
"""


def test_extract_article_links():
    links = extract_article_links(ENTRY_HTML)
    assert links == [
        "https://news.mydrivers.com/1/1149/1149297.htm",
        "https://news.mydrivers.com/1/1149/1149302.htm",
        "https://news.mydrivers.com/1/1148/1148111.htm",
    ]


def test_extract_article_links_dedup_and_empty():
    assert extract_article_links("") == []
    twice = '<a href="//news.mydrivers.com/1/1/2.htm">a</a><a href="//news.mydrivers.com/1/1/2.htm">b</a>'
    assert extract_article_links(twice) == ["https://news.mydrivers.com/1/1/2.htm"]


def test_extract_news_id():
    assert extract_news_id("https://news.mydrivers.com/1/1149/1149297.htm") == "1149297"
    assert extract_news_id("https://www.ithome.com/0/998/992.htm") is None


def test_parse_breadcrumb():
    soup = BeautifulSoup(BREADCRUMB_HTML, "html.parser")
    assert parse_breadcrumb(soup) == ("软件之家", "桌面系统")


def test_parse_breadcrumb_abnormal():
    # 缺栏目层级（只剩推广位）→ 结构异常返回 None
    bad = '<div class="pathway"><a href="/">首页</a> &gt; <a href="/a">IT采购专业调查</a></div>'
    assert parse_breadcrumb(BeautifulSoup(bad, "html.parser")) is None
    # 多余层级（站点结构变化征兆）→ 同样拒收，不做猜测式取值
    extra = (
        '<div class="pathway"><a href="/">首页</a> &gt; <a href="/a">资讯中心</a> &gt; '
        '<a href="/b">软件之家</a> &gt; <a href="/c">桌面系统</a> &gt; '
        '<a href="/d">子栏目</a> &gt; <a href="/e">IT采购专业调查</a></div>'
    )
    assert parse_breadcrumb(BeautifulSoup(extra, "html.parser")) is None
    # 无面包屑
    assert parse_breadcrumb(BeautifulSoup("<div></div>", "html.parser")) is None


def test_page_time_re():
    m = _PAGE_TIME_RE.search("出处：快科技 2026-09-07 19:38:32 作者：某人")
    assert m.group(1, 2, 3, 4, 5) == ("2026", "09", "07", "19", "38")
    # 无秒的形态也能匹配
    assert _PAGE_TIME_RE.search("2026-9-7 19:38") is not None
    assert _PAGE_TIME_RE.search("没有时间") is None


def test_publish_time_is_utc():
    from app.crawler.mydrivers import SH_TZ

    m = _PAGE_TIME_RE.search("2026-09-07 19:38:32")
    published = datetime(
        int(m.group(1)), int(m.group(2)), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), tzinfo=SH_TZ,
    ).astimezone(timezone.utc)
    assert published.hour == 11  # 北京 19:38 → UTC 11:38


def test_clean_content():
    assert clean_content("a  b\n\nc") == "a b c"
    assert clean_content("") == ""


def _sample_article_html() -> str:
    return (Path(__file__).parent / "fixtures" / "kj_article_sample.html").read_text(
        encoding="utf-8"
    )


def test_parse_article_html_full_pipeline():
    """真实页面结构样本 → 标题/时间/正文/面包屑端到端解析（站点改版时的回归哨兵）。"""
    parsed = _parse_article_html(_sample_article_html())
    assert parsed is not None
    title, published, content, (category, channel) = parsed
    assert title == "8GB内存PC性能实测：Win11真是拖后腿 空闲就吃掉85%内存"
    assert (published.hour, published.minute) == (11, 38)  # 北京 19:38 → UTC
    assert published.tzinfo == timezone.utc
    assert (category, channel) == ("软件之家", "桌面系统")
    assert content.startswith("快科技9月7日消息")  # 导语按窄口径保留
    assert "日本媒体就在8GB内存" in content
    # 正文不含标签页/面包屑等非正文内容
    assert "IT采购专业调查" not in content and "Windows 11" not in content.split("消息，")[0]


def test_parse_article_html_missing_body_rejected():
    html = re.sub(r'<div class="news_info">.*?</div>', "", _sample_article_html(), flags=re.S)
    assert _parse_article_html(html) is None  # 正文容器缺失：拒收而非入库半截数据


def test_whitelist_covers_kj_software_column():
    """白名单 = 快科技「软件之家」大类全部二级栏目（官网 windows/ruanjian 栏目页口径）。

    移动应用/手机系统 2026-09-10 加入：官网软件栏目列表含微信/App 文与鸿蒙/OriginOS/
    MagicOS 系统文，不收则与官网栏目页内容对不上；对齐 IT之家软件之家频道口径。
    """
    assert CHANNEL_WHITELIST == {"桌面系统", "电脑软件", "移动应用", "手机系统"}


def test_column_pages_are_entry_points():
    """官网两栏目页必须作为主入口：全站流仅覆盖最新约半天，栏目文大多不在其中
    （2026-09-10 实测 windows.html 16 篇栏目文 0 篇出现在 technewsall）。"""
    for url in ENTRY_PAGES[:2]:
        assert url.endswith(("windows.html", "ruanjian.html")), url


FEED_XML = """
<?xml version="1.0" encoding="utf-8"?><rss><channel>
<item><title>豆包输入法Windows版正式上线</title>
<link>https://news.mydrivers.com/1/1149/1149629.htm</link>
<pubDate>2026-09-08 22:32:18</pubDate></item>
<item><title>另一篇</title>
<link>https://news.mydrivers.com/1/1149/1149505.htm</link>
<pubDate>2026-09-08 16:12:22</pubDate></item>
<item><title>重复链接应被去重</title>
<link>https://news.mydrivers.com/1/1149/1149629.htm</link>
<pubDate>2026-09-08 22:32:18</pubDate></item>
<item><title>非文章链接应被忽略</title>
<link>https://www.mydrivers.com/other.htm</link></item>
</channel></rss>
"""


def test_extract_feed_items():
    items = extract_feed_items(FEED_XML)
    assert [u.rsplit("/", 1)[-1] for u, _ in items] == [
        "1149629.htm",
        "1149505.htm",
    ]
    # feed 发布时间解析为 UTC（北京时间 22:32 → UTC 14:32）
    assert items[0][1] is not None and items[0][1].hour == 14


def test_extract_feed_items_empty():
    assert extract_feed_items("") == []
    assert extract_feed_items("<rss><channel></channel></rss>") == []


def test_extract_feed_items_pubdate_field_not_body_text():
    """发布时间只认 <pubDate> 字段：description 等字段里的日期样文本不得误当
    发布时间（否则窗口预过滤会静默丢文）。"""
    xml = """<rss><channel>
<item><title>t</title>
<link>https://news.mydrivers.com/1/1149/1149700.htm</link>
<description>回顾 2019-05-01 10:00 的旧闻与新计划</description>
<pubDate>2026-09-10 09:30:00</pubDate></item>
</channel></rss>"""
    items = extract_feed_items(xml)
    assert len(items) == 1 and items[0][0].endswith("1149700.htm")
    # pubDate 2026-09-10 09:30 北京时间 → UTC 01:30；若误取 description 里的
    # 2019-05-01 则文章会被窗口预过滤丢弃
    assert items[0][1] is not None and items[0][1].year == 2026
