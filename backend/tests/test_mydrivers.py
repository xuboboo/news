"""快科技爬虫纯函数单测：链接提取 / news_id / 面包屑解析 / 时间解析 / 正文清洗。

样本片段取自 2026-09 实抓文章页（news.mydrivers.com/1/1149/1149297.htm）与入口页结构。
"""
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from app.crawler.mydrivers import (
    _PAGE_TIME_RE,
    clean_content,
    extract_article_links,
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
