"""快科技（mydrivers.com）采集服务（2026-09 接入，第二数据源）。

栏目真相在文章详情页的面包屑：

    首页 > 资讯中心 > 软件之家 > 桌面系统 > IT采购专业调查(固定推广位，丢弃)
                                 ↑一级栏目   ↑二级栏目 = 频道真相

采集流程：栏目页（windows.html / ruanjian.html，官网编辑维护的栏目真相列表）
+ 全站最新流 technewsall.html（广度兜底）+ 官方 RSS「软件相关」feed（深度兜底）
→ 逐篇进文章页解析标题/时间/正文/面包屑 → 面包屑二级栏目落在白名单内的才入库，
其余看完即弃（面包屑判定需要请求文章页，白名单外文章每篇消耗一次请求，量级可接受）。

入口演进（2026-09-10 实测修正）：早期结论"栏目页链接全是全站流子集"已失效——
全站发文速度快，technewsall 单页约 133 条仅覆盖最新半天到一天，栏目文大多发布后
即滚出全站流窗口（当日实测 windows.html 16 篇栏目文 0 篇在 technewsall 中）；
feed cid=8 更新滞后（实测 27h+ 无新条目）且不含 Windows 文。栏目页因此升为主入口：
页面顶部约 12 条是全站推荐位（混各栏目），下半部分才是本栏目真实列表，
整页提取链接靠面包屑白名单兜底过滤，不做容器级解析以抗改版。

白名单（与 IT之家四频道口径对齐，2026-09 定稿）：
- 桌面系统（软件之家 > 桌面系统）：Windows/Win11 相关，对齐 Win11之家/Win10之家
- 电脑软件（软件之家 > 电脑软件）：软件与 AI 应用，对齐 软件之家/智能时代
- 移动应用（软件之家 > 移动应用）：微信/手机 App（2026-09-10 加）——官网 ruanjian
  栏目列表含此类文章，对齐 IT之家软件之家频道同收微信/App 文的口径
- 手机系统（软件之家 > 手机系统）：鸿蒙/OriginOS/MagicOS 等（2026-09-10 加）——
  同样在官网 ruanjian 栏目列表展示，不收则与官网软件栏目页内容对不上
扩展栏目 = 在 CHANNEL_WHITELIST 加一行。

热度口径（2026-09 决策）：本期不启用快科技阅读数。人气接口已验证可用
（count.mydrivers.com/HitsUTF8.aspx?Tid=<news_id> 返回 `var hits =2473;`），
但与 IT之家互动数据（评论/点赞）量纲差异大，直接混榜会系统性压制 IT之家文章；
views 等互动字段入库一律写 0，热榜维持时间衰减，留待分来源归一化专项再启用。
stats_next_poll_at 保持 None，互动轮询（crawler/stats.py）按 source 过滤不会误捞。
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from app.crawler.ithome import (
    SH_TZ,
    _decode_response,
    _fetch,
    _insert_article,
)
from app.models.article import Article

logger = logging.getLogger(__name__)

SOURCE_NAME = "快科技"

# 采集入口：栏目页（官网编辑维护的栏目真相列表，2026-09-10 升为主入口）+ 全站流（广度兜底）
# + 官方 RSS「软件相关」feed（深度兜底）。栏目页顶部约 12 条是全站推荐位（混各栏目），
# 下半部分才是本栏目真实列表——整页提取链接、面包屑白名单兜底过滤，不做容器级解析以抗改版。
# feed 为官方服务（rss.mydrivers.com，robots.txt 允许抓取，仅 /zhibo/ 禁抓）：
# 实测更新滞后（27h+ 无新条目）且不含 Windows 文，仅作窗口内深度捞回的兜底线索。
ENTRY_PAGES: list[str] = [
    "https://news.mydrivers.com/windows.html",
    "https://news.mydrivers.com/ruanjian.html",
    "https://news.mydrivers.com/technewsall.html",
]
FEED_ENTRIES: list[str] = [
    "https://rss.mydrivers.com/rss.aspx?cid=8",  # 资讯：软件相关
]

# 面包屑二级栏目白名单：落在名单内的文章才入库，channel 即栏目名。
# 桌面系统/电脑软件/移动应用/手机系统 = 快科技「软件之家」大类全部二级栏目（2026-09-10
# 实抓官网 ruanjian/windows 栏目页归纳），与 IT之家「软件之家」频道口径对齐。
CHANNEL_WHITELIST: set[str] = {"桌面系统", "电脑软件", "移动应用", "手机系统"}

# 与 ithome 同口径：栏目/入口页会混入旧文，仅收录时间窗口内的文章
_PAGE_MAX_AGE_HOURS = 24 * 14

# 入口页链接上限：全站流单页实测 117-129 篇，独立常量而非 crawl_limit_per_feed 派生——
# 该配置是面板热调项（为 ithome 每页 20-35 篇设计），误调小会让白名单文章（常在页面后段）静默漏采
_ENTRY_LINK_CAP = 200

# 轮内请求间隔：首轮回填约 120 篇文章页连发，留礼貌间隔降低 403 风险
# （ithome 侧未加是历史现状；403/429 兜底走 _fetch 的域名退避）
_POLITENESS_SLEEP_SECONDS = 0.3

# 页面链接近期检查缓存（进程内 TTL）：独立于 ithome 的一份，防每轮重复抓同一批旧文
_LINK_CHECK_TTL_SECONDS = 2 * 3600
_LINK_CHECKED: dict[str, float] = {}

# https://news.mydrivers.com/1/1149/1149297.htm → news_id 1149297（末段即文章 ID，
# 与人气/评论接口的 Tid 一致；首段 1149 是 id//1000 的目录位）
_NID = re.compile(r"mydrivers\.com/1/\d+/(\d+)\.htm")

_ARTICLE_HREF = re.compile(
    r'href="((?:https:)?//news\.mydrivers\.com/1/\d+/\d+\.htm)(?:[?#][^"]*)?"'
)

# RSS feed <item> 内的链接与发布时间（feed 为 UTF-8 XML，链接是绝对地址）
_FEED_LINK_RE = re.compile(r"<link>(https://news\.mydrivers\.com/1/\d+/\d+\.htm)</link>")

# 文章页元信息行（.news_bt1_left）内的发布时间，如「2026-09-07 19:38:32」（北京时间）
_PAGE_TIME_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")

# 面包屑固定层级：前两级是站点路径，末级是固定推广位（不随文章变化）
_BREADCRUMB_FIXED = {"首页", "资讯中心", "IT采购专业调查"}


def extract_news_id(link: str) -> str | None:
    """文章链接 → 末段数字 ID（如 1149297）。"""
    m = _NID.search(link or "")
    return m.group(1) if m else None


def extract_article_links(html: str) -> list[str]:
    """入口页里的文章链接，规范化为 https://news.mydrivers.com/1/... 绝对地址。

    快科技页面 href 为协议相对形式（//news.mydrivers.com/1/...），保持出现顺序去重。
    """
    links: list[str] = []
    for raw in _ARTICLE_HREF.findall(html or ""):
        link = raw if raw.startswith("https:") else "https:" + raw
        if link not in links:
            links.append(link)
    return links


def extract_feed_items(xml: str) -> list[tuple[str, datetime | None]]:
    """RSS feed XML → [(文章链接, feed 发布时间 | None)]，保持出现顺序去重。

    feed 发布时间（北京时间）仅用于跳过超窗口旧文的预过滤，省一次文章页请求；
    入库发布时间仍以文章页元信息行为准。
    """
    items: list[tuple[str, datetime | None]] = []
    seen: set[str] = set()
    for item_body in re.findall(r"<item>.*?</item>", xml or "", re.S):
        m = _FEED_LINK_RE.search(item_body)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        published = None
        t = _PAGE_TIME_RE.search(item_body)
        if t:
            try:
                published = datetime(
                    int(t.group(1)), int(t.group(2)), int(t.group(3)),
                    int(t.group(4)), int(t.group(5)), tzinfo=SH_TZ,
                ).astimezone(timezone.utc)
            except ValueError:
                published = None
        items.append((m.group(1), published))
    return items


def parse_breadcrumb(soup: BeautifulSoup) -> tuple[str, str] | None:
    """解析文章页面包屑 → (一级栏目, 二级栏目)。

    剔除固定层级（首页/资讯中心/推广位）后必须恰好剩两级；结构异常（缺失/多余）
    一律返回 None——多余层级往往意味着站点结构变化，靠白名单兜底不可靠。
    """
    node = soup.select_one("div.pathway")
    if node is None:
        return None
    labels = [a.get_text(strip=True) for a in node.select("a")]
    levels = [x for x in labels if x and x not in _BREADCRUMB_FIXED]
    if len(levels) != 2:
        return None
    return levels[0], levels[1]


def clean_content(text: str) -> str:
    """正文清洗：空白归一。

    导语「快科技X月X日消息，」与 IT之家「IT之家 X月X日消息」同款约定，
    按 event_service 窄口径惯例保留（不算脏数据），不做剥离。
    """
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_article_html(text: str) -> tuple[str, datetime, str, tuple[str, str]] | None:
    """解析文章页 HTML → (标题, 发布时间, 正文, (一级栏目, 二级栏目))；结构缺失返回 None。

    纯解析、不做网络请求：抓取由调用方负责——网络失败是瞬态（不该写链接缓存），
    与结构解析失败（确定性）语义不同。
    """
    soup = BeautifulSoup(text, "html.parser")

    breadcrumb = parse_breadcrumb(soup)
    if breadcrumb is None:
        return None

    # 标题：快科技标题容器 → h1 → og:title 三级回退（与 ithome 同策略）
    title = ""
    node = soup.select_one("#thread_subject")
    if node:
        title = node.get_text(strip=True)
    if not title:
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = og.get("content", "").strip() if og else ""
    if not title:
        return None

    # 发布时间优先取元信息行（.news_bt1_left），避免误匹配页面上其他日期文本；
    # 行缺失或行内无时间时回退全文首个时间文本（与 ithome 同策略）
    meta = soup.select_one(".news_bt1_left")
    m = _PAGE_TIME_RE.search(meta.get_text(" ", strip=True)) if meta else None
    if m is None:
        m = _PAGE_TIME_RE.search(text)
    if m is None:
        return None
    try:
        published = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)),
            tzinfo=SH_TZ,
        ).astimezone(timezone.utc)
    except ValueError:
        return None

    body = soup.select_one("div.news_info")
    content = clean_content(body.get_text(" ", strip=True)) if body else ""
    if not content:
        # 正文容器缺失（站点改版征兆）：拒收，避免入库"只有标题"的半截数据
        return None
    return title, published, content, breadcrumb


def _prune_link_cache(now_ts: float) -> None:
    """进程内缓存容量护栏（与 ithome 同策略）。"""
    if len(_LINK_CHECKED) > 5000:
        for k in [k for k, ts in _LINK_CHECKED.items() if now_ts - ts >= _LINK_CHECK_TTL_SECONDS]:
            _LINK_CHECKED.pop(k, None)


def _ingest_links(
    db,
    links: list[str],
    new_ids: list[int],
    per_channel: dict[str, int],
) -> dict:
    """逐篇处理候选链接：查库去重 → 抓文章页 → 解析 → 白名单过滤 → 入库。

    返回统计：scanned=新入库，skipped=已在库/过期/解析失败等，filtered=白名单外丢弃。
    已入库文章的频道以面包屑为准不再重判（重判需重复抓文章页，成本不值）。
    """
    now_ts = time.time()
    now = datetime.now(timezone.utc)
    _prune_link_cache(now_ts)
    scanned = skipped = filtered = 0

    for link in links:
        if db.query(Article).filter_by(url=link).first():
            _LINK_CHECKED[link] = now_ts
            skipped += 1
            continue

        # 近期已确认处理的链接，本进程内跳过重复抓取
        last = _LINK_CHECKED.get(link)
        if last and now_ts - last < _LINK_CHECK_TTL_SECONDS:
            skipped += 1
            continue

        # 抓取与解析分离：网络失败（超时/限流退避）是瞬态，不写链接缓存，
        # 否则轮中一次 403 触发域名退避后，当轮剩余链接会被集体标记"已处理"拉黑 2 小时
        resp = _fetch(link)
        if resp is None or resp.status_code != 200:
            continue
        time.sleep(_POLITENESS_SLEEP_SECONDS)
        parsed = _parse_article_html(_decode_response(resp))
        _LINK_CHECKED[link] = now_ts  # 页面已取到，解析结论是确定性的
        if parsed is None:
            skipped += 1
            continue
        title, published, content, (category, channel) = parsed

        # 站点时钟异常/时间正则误匹配的防御：未来时间直接拒收
        if published > now + timedelta(minutes=10):
            skipped += 1
            continue

        if channel not in CHANNEL_WHITELIST:
            filtered += 1
            logger.info("filtered by whitelist %s > %s: %s", category, channel, link)
            continue

        # 面包屑含历史文章：超出时间窗口的旧文不入库
        age_h = (now - published).total_seconds() / 3600
        if age_h > _PAGE_MAX_AGE_HOURS:
            skipped += 1
            continue

        article = Article(
            title=title[:500],
            url=link,
            channel=channel,  # 面包屑即频道真相
            source=SOURCE_NAME,
            news_id=extract_news_id(link),
            content=content[:20000],
            publish_time=published,
            views=0,
            comments=0,
            shares=0,
            likes=0,
            # 热度口径（见模块 docstring）：本期不启用阅读数，互动轮询不排期
            stats_next_poll_at=None,
            ai_status="pending",
        )
        before = len(new_ids)
        _insert_article(db, article, new_ids)
        if len(new_ids) > before:
            scanned += 1
            per_channel[channel] = per_channel.get(channel, 0) + 1
        else:
            skipped += 1

    logger.info(
        "link batch done: links=%s new=%s skipped=%s filtered=%s",
        len(links), scanned, skipped, filtered,
    )
    return {"scanned": scanned, "skipped": skipped, "filtered": filtered}


def _ingest_entry_page(
    db,
    page_url: str,
    new_ids: list[int],
    per_channel: dict[str, int],
) -> dict:
    """抓单个入口页（HTML 全站流）→ 解析出文章链接 → _ingest_links。"""
    resp = _fetch(page_url)
    if resp is None or resp.status_code != 200:
        logger.warning("entry page unreachable: %s", page_url)
        return {"scanned": 0, "skipped": 0, "filtered": 0}

    links = extract_article_links(_decode_response(resp))[:_ENTRY_LINK_CAP]
    return _ingest_links(db, links, new_ids, per_channel)


def _ingest_feed(
    db,
    feed_url: str,
    new_ids: list[int],
    per_channel: dict[str, int],
) -> dict:
    """抓单个官方 RSS feed → 解析 item 链接（feed 时间预过滤超窗口旧文）→ _ingest_links。

    feed 分类是旧驱动之家口径，与面包屑不完全对齐，故仅作发现线索；
    feed 单条约 100 条、深度远超全站流，可捞回已滚出 technewsall 的近期文章。
    """
    resp = _fetch(feed_url)
    if resp is None or resp.status_code != 200:
        logger.warning("feed unreachable: %s", feed_url)
        return {"scanned": 0, "skipped": 0, "filtered": 0}

    items = extract_feed_items(_decode_response(resp))
    window_start = datetime.now(timezone.utc) - timedelta(hours=_PAGE_MAX_AGE_HOURS)
    links = [
        link
        for link, published in items
        if published is None or published >= window_start
    ]
    logger.info("feed items %s: total=%s in-window=%s", feed_url, len(items), len(links))
    return _ingest_links(db, links, new_ids, per_channel)


def ingest_all(db) -> dict:
    """采集全部入口页并入库（白名单过滤）。返回结构与 ithome.ingest_all 同构，
    另带 filtered（白名单外丢弃数），供 task_run detail 分源展示。
    """
    new_ids: list[int] = []
    scanned = skipped = filtered = 0
    per_channel: dict[str, int] = {}

    for page_url in ENTRY_PAGES:
        result = _ingest_entry_page(db, page_url, new_ids, per_channel)
        scanned += result["scanned"]
        skipped += result["skipped"]
        filtered += result["filtered"]

    for feed_url in FEED_ENTRIES:
        result = _ingest_feed(db, feed_url, new_ids, per_channel)
        scanned += result["scanned"]
        skipped += result["skipped"]
        filtered += result["filtered"]

    db.commit()
    logger.info(
        "crawl done (%s): scanned=%s new=%s skipped=%s filtered=%s",
        SOURCE_NAME, scanned, len(new_ids), skipped, filtered,
    )
    return {
        "scanned": scanned,
        "new": len(new_ids),
        "skipped": skipped,
        "rechanneled": 0,  # 快科技频道以面包屑为准，无列表页纠偏语义，恒 0（键位兼容调用方）
        "filtered": filtered,
        "new_ids": new_ids,
        "per_channel": per_channel,
    }


def _dry_run(limit: int = 40) -> None:
    """实网验证：抓全部入口（HTML 页 + RSS feed）→ 逐篇解析，打印面包屑判定明细，不写库。"""
    from collections import Counter

    candidates: list[str] = []
    for page_url in ENTRY_PAGES:
        resp = _fetch(page_url)
        if resp is None or resp.status_code != 200:
            print(f"入口页不可达：{page_url}")
            return
        page_links = extract_article_links(_decode_response(resp))
        print(f"入口页 {page_url}：链接 {len(page_links)} 条")
        candidates.extend(page_links)
    window_start = datetime.now(timezone.utc) - timedelta(hours=_PAGE_MAX_AGE_HOURS)
    for feed_url in FEED_ENTRIES:
        resp = _fetch(feed_url)
        if resp is None or resp.status_code != 200:
            print(f"Feed 不可达：{feed_url}")
            continue
        items = extract_feed_items(_decode_response(resp))
        in_window = [link for link, pub in items if pub is None or pub >= window_start]
        print(f"Feed {feed_url}：item {len(items)} 条，窗口内 {len(in_window)} 条")
        candidates.extend(in_window)

    # 入口页与 feed 重叠部分去重（保持出现顺序）
    seen: set[str] = set()
    links = [c for c in candidates if not (c in seen or seen.add(c))][:limit]
    print(f"\n去重后候选 {len(links)} 条（上限 {limit}），开始逐篇判定：\n")

    stats = {"new": 0, "filtered": 0, "parse_fail": 0}
    channel_counter: Counter[str] = Counter()
    for link in links:
        art = _fetch(link)
        if art is None or art.status_code != 200:
            stats["parse_fail"] += 1
            print(f"  [抓取失败] {link}")
            continue
        parsed = _parse_article_html(_decode_response(art))
        if parsed is None:
            stats["parse_fail"] += 1
            print(f"  [解析失败] {link}")
            continue
        title, published, content, (category, channel) = parsed
        if channel in CHANNEL_WHITELIST:
            stats["new"] += 1
            channel_counter[channel] += 1
            verdict = f"[入库 {category} > {channel}]"
        else:
            stats["filtered"] += 1
            verdict = f"[过滤 {category} > {channel}]"
        print(f"  {verdict} {published.astimezone(SH_TZ):%m-%d %H:%M} {title[:40]}（正文 {len(content)} 字）")
    print(
        f"\n合计：链接 {len(links)} ｜ 入库 {stats['new']} ｜ 过滤 {stats['filtered']}"
        f" ｜ 解析失败 {stats['parse_fail']} ｜ 频道分布 {dict(channel_counter)}"
    )


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="快科技采集（默认写库；--dry-run 只解析不入库）")
    parser.add_argument("--dry-run", action="store_true", help="实网验证解析与白名单判定，不写库")
    parser.add_argument("--limit", type=int, default=40, help="dry-run 最多解析的文章数")
    args = parser.parse_args()

    if args.dry_run:
        _dry_run(limit=args.limit)
    else:
        from app.core.db import new_session

        session = new_session()
        try:
            print(ingest_all(session))
        finally:
            session.close()
