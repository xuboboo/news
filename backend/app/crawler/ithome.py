"""IT之家体系采集服务（PRD 数据链路第一环：数据源 → 采集服务 → 文章数据库）。

频道 → 订阅源映射（可在环境变量 FEEDS_JSON 中覆盖）：
- 智能时代：IT之家主站 RSS（分类 RSS 已实测下线）
- 软件之家 / Win11之家 / Win10之家：频道标签页（/tags/Win11/ 等，实测可用）
  → 解析文章页 h1 标题 / 发布时间 / 正文入库
- 主站源文章按关键词归类（保证频道字段真实有据）
"""
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.article import Article

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

try:
    SH_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # 极端精简镜像缺 tzdata 时的固定偏移兜底
    from datetime import timedelta

    SH_TZ = timezone(timedelta(hours=8))

FEEDS: dict[str, list[str]] = {
    "智能时代": ["https://www.ithome.com/rss/"],
    # 分类 RSS（/rss/win11.xml 等）已被站点下线（404），改用标签页源，见 TAG_SOURCES
}

# 频道标签页源（实测可用，含最新文章链接）
TAG_SOURCES: dict[str, str] = {
    "软件之家": "https://www.ithome.com/tags/软件/",
    "Win11之家": "https://www.ithome.com/tags/Win11/",
    "Win10之家": "https://www.ithome.com/tags/Win10/",
}

# ——— 源级限流退避：403/429 后暂停该域名一段时间，指数递增，避免硬抓被封 ———
_FETCH_BACKOFF: dict[str, float] = {}   # host → 退避截止时间戳
_BACKOFF_FAILS: dict[str, int] = {}     # host → 连续限流次数
_BACKOFF_BASE_SECONDS = 30 * 60         # 首次 30 分钟
_BACKOFF_MAX_SECONDS = 2 * 3600         # 上限 2 小时

# 标签页会混入旧文：仅收录该时间窗口内的文章，避免污染时间轴。
# 14 天而非 72h：慢更频频道（如 Win10之家）最新文章常在数天前，
# 窗口太紧会导致整频道长期为空；更早的旧文仍会被拒。
_TAG_MAX_AGE_HOURS = 24 * 14

# 标签页链接近期检查缓存（进程内 TTL）：避免每轮采集重复抓取同一批旧文
_TAG_CHECK_TTL_SECONDS = 2 * 3600
_TAG_CHECKED: dict[str, float] = {}

# 分类源不可达时的关键词归类（兜底，保证频道字段真实有据）
CHANNEL_RULES: list[tuple[str, list[str]]] = [
    ("Win11之家", ["win11", "windows 11", "win11"]),
    ("Win10之家", ["win10", "windows 10", "win10"]),
    ("软件之家", ["软件", "浏览器", "edge", "微信", "qq", "wps", "应用", "office", "记事本"]),
    ("智能时代", ["ai", "大模型", "芯片", "手机", "汽车", "航天", "openai", "英伟达", "华为", "苹果", "iphone"]),
]

_NID = re.compile(r"ithome\.com/0/(\d+)/(\d+)\.htm")


def extract_news_id(link: str) -> str | None:
    """https://www.ithome.com/0/998/992.htm → newsid 998992"""
    m = _NID.search(link or "")
    if m:
        return m.group(1) + m.group(2)
    return None


# 正文清洗：致谢行 / 广告声明等非正文事实（入库与摘要共用）
_JUNK_PATTERNS = [
    re.compile(r"感谢IT之家网友[^。！？!?]*线索投递\s*[!！。]?\s*"),
    re.compile(r"广告声明：文内含有的对外跳转链接[^\n]*$"),
]


def clean_content(text: str) -> str:
    """剥离致谢行与广告声明，返回干净正文。"""
    for pat in _JUNK_PATTERNS:
        text = pat.sub(" ", text or "")
    return re.sub(r"\s+", " ", text or "").strip()


def _classify_channel(title: str) -> str:
    low = title.lower()
    for channel, words in CHANNEL_RULES:
        if any(w in low for w in words):
            return channel
    return "智能时代"


def _insert_article(db: Session, article: Article, new_ids: list[int]) -> None:
    """SAVEPOINT 内插入单篇文章：URL 唯一约束冲突（并发爬虫已抢先入库）只跳过该篇，
    不让单条 IntegrityError 回滚整轮采集。"""
    try:
        with db.begin_nested():
            db.add(article)
            db.flush()
    except IntegrityError:
        logger.info("skip duplicated url (concurrent insert): %s", article.url)
        return
    new_ids.append(article.id)


def _fetch(url: str) -> httpx.Response | None:
    """带源级退避的抓取：403/429 后自动暂停该域名（30min 起指数递增，上限 2h）。"""
    host = urlsplit(url).netloc
    now_ts = time.time()
    until = _FETCH_BACKOFF.get(host, 0)
    if until > now_ts:
        logger.info("backoff skip %s（剩约 %d 分钟）", host, int((until - now_ts) / 60))
        return None
    try:
        resp = httpx.get(url, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
    except Exception as exc:
        logger.warning("fetch failed %s: %s", url, exc)
        return None
    if resp.status_code in (403, 429):
        fails = _BACKOFF_FAILS.get(host, 0) + 1
        penalty = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (fails - 1)))
        _BACKOFF_FAILS[host] = fails
        _FETCH_BACKOFF[host] = now_ts + penalty
        logger.warning(
            "rate-limited by %s (HTTP %s)，暂停 %d 分钟（连续第 %d 次）",
            host, resp.status_code, penalty // 60, fails,
        )
        return None
    # 成功即重置该域名的退避状态
    _BACKOFF_FAILS.pop(host, None)
    _FETCH_BACKOFF.pop(host, None)
    return resp


def fetch_content(news_id: str | None, rss_html: str, link: str = "") -> str:
    """正文：优先抓文章页 #paragraph 主容器，失败回退 RSS 摘要；统一清洗致谢行等杂质。"""
    text = ""
    if link:
        resp = _fetch(link)
        if resp is not None and resp.status_code == 200:
            try:
                soup = BeautifulSoup(resp.text, "html.parser")
                node = soup.select_one("#paragraph") or soup.select_one(".post_content")
                if node:
                    text = node.get_text(" ", strip=True)
            except Exception:
                pass
    if not text or len(text) <= 60:
        text = _html_to_text(rss_html or "") or text
    return clean_content(text)


def fetch_stats(news_id: str | None) -> tuple[int, int, int]:
    """互动数据：站点公开统计接口已下线（实测 404），字段保留为 0，
    热度算法自动降级为时间衰减主导；未来接入新数据源后自动恢复。"""
    return 0, 0, 0


def _html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(" ", strip=True)
    except Exception:
        return html


def _parse_publish(entry) -> datetime | None:
    """RSS 条目发布时间；无任何日期字段时返回 None（调用方跳过该条，
    绝不能用 now() 充数——会污染热度衰减、时间轴与 14 天入库窗口）。"""
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    return None


# 文章页内的发布时间文本，如「2026/9/6 18:51」（北京时间）
_PAGE_TIME_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")


def _parse_article_page(link: str) -> tuple[str, datetime, str] | None:
    """解析文章页：返回 (标题, 发布时间, 正文)；失败返回 None。"""
    resp = _fetch(link)
    if resp is None or resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.select_one("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = og.get("content", "").strip() if og else ""
    if not title:
        return None
    m = _PAGE_TIME_RE.search(resp.text)
    if not m:
        return None
    try:
        published = datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)), tzinfo=SH_TZ,
        ).astimezone(timezone.utc)
    except ValueError:
        return None
    node = soup.select_one("#paragraph") or soup.select_one(".post_content")
    content = node.get_text(" ", strip=True) if node else ""
    return title, published, clean_content(content)


def _ingest_tag_channel(db: Session, channel: str, seen_urls: set[str]) -> dict:
    """分类 RSS 失效时的降级采集：抓频道标签页 → 解析新文章入库。"""
    tag_url = TAG_SOURCES[channel]
    resp = _fetch(tag_url)
    if resp is None or resp.status_code != 200:
        logger.warning("tag page unreachable: %s (%s)", tag_url, channel)
        return {"scanned": 0, "skipped": 0, "new_ids": []}

    links = list(dict.fromkeys(
        re.findall(r'href="(https://www\.ithome\.com/0/\d+/\d+\.htm)"', resp.text)
    ))
    now_ts = time.time()
    now = datetime.now(timezone.utc)
    # 进程内缓存容量护栏：只保留 TTL 内的条目，防止长期运行内存无界增长
    if len(_TAG_CHECKED) > 5000:
        for k in [k for k, ts in _TAG_CHECKED.items() if now_ts - ts >= _TAG_CHECK_TTL_SECONDS]:
            _TAG_CHECKED.pop(k, None)
    scanned = skipped = rechanneled = 0
    new_ids: list[int] = []

    for link in links[: get_settings().crawl_limit_per_feed * 2]:
        if link in seen_urls:
            # 本轮主站源已处理过该链接：仍做一次廉价 DB 检查，
            # 若已入库且频道与标签页不一致则纠正（主站 RSS 关键词归类可能归错）
            exists = db.query(Article).filter_by(url=link).first()
            if exists and exists.channel != channel:
                exists.channel = channel
                rechanneled += 1
            continue
        seen_urls.add(link)

        if exists := db.query(Article).filter_by(url=link).first():
            _TAG_CHECKED[link] = now_ts
            # 标签页归属权威：已入库文章若频道不一致则纠正
            # （首次经主站 RSS 关键词归类，标题无关键词时会误归"智能时代"）
            if exists.channel != channel:
                exists.channel = channel
                rechanneled += 1
            skipped += 1
            continue

        # 近期已确认无新内容的链接，本进程内跳过重复抓取
        last = _TAG_CHECKED.get(link)
        if last and now_ts - last < _TAG_CHECK_TTL_SECONDS:
            skipped += 1
            continue

        parsed = _parse_article_page(link)
        _TAG_CHECKED[link] = now_ts
        if parsed is None:
            continue
        title, published, content = parsed

        # 标签页含历史文章：超出时间窗口的旧文不入库
        age_h = (now - published).total_seconds() / 3600
        if age_h > _TAG_MAX_AGE_HOURS:
            skipped += 1
            continue

        article = Article(
            title=title[:500],
            url=link,
            channel=channel,  # 标签页频道归属明确，无需关键词归类
            source="IT之家",
            news_id=extract_news_id(link),
            content=content[:20000],
            publish_time=published,
            views=0,
            comments=0,
            shares=0,
            ai_status="pending",
        )
        before = len(new_ids)
        _insert_article(db, article, new_ids)
        if len(new_ids) > before:
            scanned += 1
        else:
            skipped += 1

    logger.info(
        "tag ingest %s: links=%s scanned=%s new=%s skipped=%s rechanneled=%s",
        channel, len(links), scanned, len(new_ids), skipped, rechanneled,
    )
    return {
        "scanned": scanned,
        "skipped": skipped,
        "new_ids": new_ids,
        "rechanneled": rechanneled,
    }


def ingest_all(db: Session, channel: str | None = None) -> dict:
    """采集所有订阅源并入库（分类 RSS 失效时自动降级标签页抓取）。返回统计与新文章 ID 列表。"""
    s = get_settings()
    feeds = FEEDS if channel is None else {channel: FEEDS.get(channel, [])}
    # 标签页频道（分类 RSS 已下线）也要参与采集循环：空 RSS 列表会触发标签页降级
    for ch in TAG_SOURCES:
        if (channel is None or channel == ch) and ch not in feeds:
            feeds[ch] = []

    new_ids: list[int] = []
    scanned = skipped = rechanneled = 0
    seen_urls: set[str] = set()

    for ch, urls in feeds.items():
        feed_ok = False
        for url in urls:
            resp = _fetch(url)
            if resp is None or resp.status_code != 200 or not resp.content:
                logger.warning("feed unreachable: %s (%s)", url, ch)
                continue
            feed_ok = True
            feed = feedparser.parse(resp.content)

            for entry in feed.entries[: s.crawl_limit_per_feed]:
                link = getattr(entry, "link", "") or ""
                title = (getattr(entry, "title", "") or "").strip()
                if not link or not title or link in seen_urls:
                    continue
                seen_urls.add(link)
                scanned += 1

                exists = db.query(Article).filter_by(url=link).first()
                if exists:
                    skipped += 1
                    continue

                nid = extract_news_id(link) or getattr(entry, "id", None)
                summary_html = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                published = _parse_publish(entry)
                if published is None:
                    # 无发布时间的条目不入库（无法参与热度衰减与时间窗口过滤）
                    skipped += 1
                    continue

                content = fetch_content(nid, summary_html, link)
                views, comments, shares = fetch_stats(nid)
                feed_channel = ch
                if url == FEEDS["智能时代"][0]:
                    feed_channel = _classify_channel(title)

                article = Article(
                    title=title[:500],
                    url=link,
                    channel=feed_channel,
                    source="IT之家",
                    news_id=str(nid) if nid else None,
                    content=content[:20000],
                    publish_time=published,
                    views=views,
                    comments=comments,
                    shares=shares,
                    ai_status="pending",
                )
                _insert_article(db, article, new_ids)
                if new_ids and new_ids[-1] == article.id:
                    scanned += 1
                else:
                    skipped += 1

        # 降级链第 2 级：该频道所有分类 RSS 均不可达 → 标签页抓取
        if not feed_ok and ch in TAG_SOURCES:
            tag_result = _ingest_tag_channel(db, ch, seen_urls)
            scanned += tag_result["scanned"]
            skipped += tag_result["skipped"]
            rechanneled += tag_result.get("rechanneled", 0)
            new_ids.extend(tag_result["new_ids"])

    db.commit()
    logger.info(
        "crawl done: scanned=%s new=%s skipped=%s rechanneled=%s",
        scanned, len(new_ids), skipped, rechanneled,
    )
    return {
        "scanned": scanned,
        "new": len(new_ids),
        "skipped": skipped,
        "rechanneled": rechanneled,
        "new_ids": new_ids,
    }
