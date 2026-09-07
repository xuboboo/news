"""IT之家体系采集服务（PRD 数据链路第一环：数据源 → 采集服务 → 文章数据库）。

频道 → 页面映射（页面即频道真相，不做任何关键词归类）：
- Win11之家：官方子站 win11.ithome.com（/tags/Win11/ 标签页兜底）
- Win10之家：官方子站 win10.ithome.com（/tags/Win10/ 标签页兜底）
- 软件之家：官方子站 soft.ithome.com（/tags/软件/ 已 404，无兜底）
- 智能时代：官方 AI 标签页 /tags/AI/ 与 ChatGPT 标签页 /tags/ChatGPT/
  （IT之家无"智能时代"子站，取最接近的官方 AI 主题页）

文章只从上述页面收录：出现在哪个页面就属于哪个频道；
未出现在任何频道页面的文章一律不入库（2026-09 审计决策：废弃主站 RSS 全量抓取 + 关键词归类）。
"""
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

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

# 频道页面源：顺序即优先级（同一文章出现在多个页面时先到先得）。
# 子站主打最新文章，标签页兜底；每个页面各含 20-35 篇最新文章链接。
PAGE_SOURCES: dict[str, list[str]] = {
    "Win11之家": [
        "https://win11.ithome.com/",
        "https://www.ithome.com/tags/Win11/",
    ],
    "Win10之家": [
        "https://win10.ithome.com/",
        "https://www.ithome.com/tags/Win10/",
    ],
    "软件之家": [
        "https://soft.ithome.com/",
    ],
    "智能时代": [
        "https://www.ithome.com/tags/AI/",
        "https://www.ithome.com/tags/ChatGPT/",
    ],
}

# ——— 源级限流退避：403/429 后暂停该域名一段时间，指数递增，避免硬抓被封 ———
_FETCH_BACKOFF: dict[str, float] = {}   # host → 退避截止时间戳
_BACKOFF_FAILS: dict[str, int] = {}     # host → 连续限流次数
_BACKOFF_BASE_SECONDS = 30 * 60         # 首次 30 分钟
_BACKOFF_MAX_SECONDS = 2 * 3600         # 上限 2 小时

# 频道页面会混入旧文：仅收录该时间窗口内的文章，避免污染时间轴。
# 14 天而非 72h：慢更频频道（如 Win10之家）最新文章常在数天前，
# 窗口太紧会导致整频道长期为空；更早的旧文仍会被拒。
_PAGE_MAX_AGE_HOURS = 24 * 14

# 页面链接近期检查缓存（进程内 TTL）：避免每轮采集重复抓取同一批旧文
_LINK_CHECK_TTL_SECONDS = 2 * 3600
_LINK_CHECKED: dict[str, float] = {}

_NID = re.compile(r"ithome\.com/0/(\d+)/(\d+)\.htm")

_ARTICLE_HREF = re.compile(r'href="([^"]*/0/\d+/\d+\.htm)(?:[?#][^"]*)?"')


def extract_news_id(link: str) -> str | None:
    """https://www.ithome.com/0/998/992.htm → newsid 998992"""
    m = _NID.search(link or "")
    if m:
        return m.group(1) + m.group(2)
    return None


def extract_article_links(html: str) -> list[str]:
    """页面里的文章链接，规范化为 https://www.ithome.com/0/... 绝对地址。

    兼容三种 href 形式：绝对 / 协议相对（//www...）/ 相对（/0/...）。
    """
    links: list[str] = []
    for raw in _ARTICLE_HREF.findall(html or ""):
        if raw.startswith("//"):
            raw = "https:" + raw
        elif raw.startswith("/"):
            raw = "https://www.ithome.com" + raw
        elif raw.startswith("http://"):
            raw = "https://" + raw[len("http://"):]
        if raw.startswith("https://www.ithome.com/0/"):
            links.append(raw)
    return list(dict.fromkeys(links))


# ——— 正文清洗：致谢行 / 广告声明等非正文事实（入库与摘要共用） ———
_JUNK_PATTERNS = [
    re.compile(r"感谢IT之家网友[^。！？!?]*线索投递\s*[!！。]?\s*"),
    re.compile(r"广告声明：文内含有的对外跳转链接[^\n]*$"),
]


def clean_content(text: str) -> str:
    """剥离致谢行与广告声明，返回干净正文。"""
    for pat in _JUNK_PATTERNS:
        text = pat.sub(" ", text or "")
    return re.sub(r"\s+", " ", text or "").strip()


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


def fetch_stats(news_id: str | None) -> tuple[int, int, int]:
    """互动数据占位：站点公开统计接口已下线（实测 404）。
    入库一律写 0，热度算法自动降级为时间衰减主导（见 hot_score.py）；
    未来接入新数据源后在 _ingest_page_channel 内恢复调用。"""
    return 0, 0, 0


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


def _prune_link_cache(now_ts: float) -> None:
    """进程内缓存容量护栏：只保留 TTL 内的条目，防止长期运行内存无界增长。"""
    if len(_LINK_CHECKED) > 5000:
        for k in [k for k, ts in _LINK_CHECKED.items() if now_ts - ts >= _LINK_CHECK_TTL_SECONDS]:
            _LINK_CHECKED.pop(k, None)


def _ingest_page_channel(
    db: Session,
    channel: str,
    page_url: str,
    assigned: dict[str, str],
    new_ids: list[int],
) -> dict:
    """抓单个频道页面 → 解析新文章入库。

    assigned：本轮已归属频道的链接（link → channel）。同一文章出现在多个页面时
    先到先得（页面优先级见 PAGE_SOURCES），保证频道归属确定性。
    """
    resp = _fetch(page_url)
    if resp is None or resp.status_code != 200:
        logger.warning("channel page unreachable: %s (%s)", page_url, channel)
        return {"scanned": 0, "skipped": 0, "rechanneled": 0}

    links = extract_article_links(resp.text)[: get_settings().crawl_limit_per_feed * 2]
    now_ts = time.time()
    now = datetime.now(timezone.utc)
    _prune_link_cache(now_ts)
    scanned = skipped = rechanneled = 0

    for link in links:
        if link in assigned:
            # 本轮更高优先级页面已处理该链接：频道先到先得，不再改动
            skipped += 1
            continue
        assigned[link] = channel

        if exists := db.query(Article).filter_by(url=link).first():
            _LINK_CHECKED[link] = now_ts
            # 页面归属权威：已入库文章若频道不一致则纠正
            if exists.channel != channel:
                logger.info("rechannel %s: %s → %s", link, exists.channel, channel)
                exists.channel = channel
                rechanneled += 1
            skipped += 1
            continue

        # 近期已确认无新内容的链接，本进程内跳过重复抓取
        last = _LINK_CHECKED.get(link)
        if last and now_ts - last < _LINK_CHECK_TTL_SECONDS:
            skipped += 1
            continue

        parsed = _parse_article_page(link)
        _LINK_CHECKED[link] = now_ts
        if parsed is None:
            continue
        title, published, content = parsed

        # 页面含历史文章：超出时间窗口的旧文不入库
        age_h = (now - published).total_seconds() / 3600
        if age_h > _PAGE_MAX_AGE_HOURS:
            skipped += 1
            continue

        article = Article(
            title=title[:500],
            url=link,
            channel=channel,  # 页面即频道真相
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
        "page ingest %s (%s): links=%s scanned=%s new=%s skipped=%s rechanneled=%s",
        channel, page_url, len(links), scanned, len(new_ids), skipped, rechanneled,
    )
    return {"scanned": scanned, "skipped": skipped, "rechanneled": rechanneled}


def ingest_all(db: Session, channel: str | None = None) -> dict:
    """采集所有频道页面并入库。只收 PAGE_SOURCES 页面内的文章，未收录不入库。

    返回统计与新文章 ID 列表（结构兼容 loop.py / tasks.py 调用方）。
    """
    channels = list(PAGE_SOURCES) if channel is None else [ch for ch in PAGE_SOURCES if ch == channel]

    new_ids: list[int] = []
    scanned = skipped = rechanneled = 0
    per_channel: dict[str, int] = {}
    assigned: dict[str, str] = {}

    for ch in channels:
        before = len(new_ids)
        for page_url in PAGE_SOURCES.get(ch, []):
            result = _ingest_page_channel(db, ch, page_url, assigned, new_ids)
            scanned += result["scanned"]
            skipped += result["skipped"]
            rechanneled += result["rechanneled"]
        per_channel[ch] = len(new_ids) - before

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
        "per_channel": per_channel,
    }
