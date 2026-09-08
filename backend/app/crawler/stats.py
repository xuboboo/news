"""IT之家互动数据轮询：评论数 + 点赞数（热度算法的真实互动输入）。

数据源（2026-09 实测可用，均为站点页面自加载的接口，无需登录/签名）：
- 评论数：GET https://cmt.ithome.com/api/comment/count?newsid=<id>
  返回 JS 片段 `ccd.innerHTML = '226'`
- 点赞（文章价值打分中的「有价值」数）：GET https://dyn.ithome.com/grade/<newsid>
  返回 JS 片段，含 价值分 / 打分人数 / 有价值 / 无价值

阅读数接口（dyn.ithome.com/api/hitcount/<id>/pc）GET/POST 均返回 200 空 body
（只写计数器），无数据源，views 保持 0。

调度模型（分层衰减节奏）：文章越新采越勤、越旧越疏，超采集窗口停采。
状态语义见 Article.stats_* 字段注释；失败绝不覆写已有数据（接口各自独立，
哪个成功更新哪个，防止把真实互动数冲掉）。
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.crawler.ithome import _FETCH_BACKOFF, _decode_response, _fetch
from app.models.article import Article

logger = logging.getLogger(__name__)

_COUNT_URL = "https://cmt.ithome.com/api/comment/count?newsid={nid}"
_GRADE_URL = "https://dyn.ithome.com/grade/{nid}"
_STATS_HOSTS = ("cmt.ithome.com", "dyn.ithome.com")

# 评论数 JS：专属正则优先，宽松回退兜底格式微调
_COUNT_RE = re.compile(r"ccd\.innerHTML\s*=\s*'(\d+)'")
_COUNT_RE_FALLBACK = re.compile(r"innerHTML\s*=\s*'(\d+)'")
# 点赞 JS：`<span class="s" id="sgrade2"><a>有价值</a><br><div>68</div></span>`
_GRADE_LIKE_RE = re.compile(r'id="sgrade2">\s*<a>有价值</a>\s*<br>\s*<div>(\d+)</div>')

# 分层采集节奏：(文章年龄上限小时, 采集间隔分钟)，取第一个命中的档
_POLL_TIERS = [
    (2.0, 30),            # 0–2h：30 分钟（互动量黄金期）
    (12.0, 60),           # 2–12h：1 小时
    (48.0, 180),          # 12–48h：3 小时
    (float("inf"), 720),  # 48h–14d：12 小时
]
_STATS_WINDOW_HOURS = 24 * 14  # 停采窗口，与 ithome._PAGE_MAX_AGE_HOURS 同口径
_FAIL_BACKOFF_BASE_MIN = 15    # 轮次失败退避：15min × 2^(n-1)，上限 2h
_FAIL_BACKOFF_MAX_MIN = 120
GIVE_UP_FAILS = 6             # 连续失败该次数后放弃（停采终态）
_POLITENESS_SLEEP_SECONDS = 0.5  # 轮内请求间隔
# 单轮批次上限：最坏 100 篇 × 17s（8s 超时×2 + 间隔）≈ 28min < 锁 TTL 30min
_MAX_BATCH = 100


def parse_comment_count(text: str) -> int | None:
    m = _COUNT_RE.search(text or "") or _COUNT_RE_FALLBACK.search(text or "")
    return int(m.group(1)) if m else None


def parse_grade_likes(text: str) -> int | None:
    m = _GRADE_LIKE_RE.search(text or "")
    return int(m.group(1)) if m else None


def next_poll_after(publish_time: datetime, now: datetime) -> datetime | None:
    """成功轮询后按分层节奏计算下次采集时间；超出停采窗口返回 None（终态）。"""
    if publish_time.tzinfo is None:
        publish_time = publish_time.replace(tzinfo=timezone.utc)
    age_h = (now - publish_time).total_seconds() / 3600
    interval_min = next((m for cap_h, m in _POLL_TIERS if age_h < cap_h), _POLL_TIERS[-1][1])
    nxt = now + timedelta(minutes=interval_min)
    window_end = publish_time + timedelta(hours=_STATS_WINDOW_HOURS)
    return nxt if nxt <= window_end else None


def next_poll_after_failure(fail_count: int, now: datetime) -> datetime | None:
    """轮次全失败后的退避时间；达到放弃阈值返回 None（终态）。fail_count 为失败后的累计值。"""
    if fail_count >= GIVE_UP_FAILS:
        return None
    minutes = min(_FAIL_BACKOFF_MAX_MIN, _FAIL_BACKOFF_BASE_MIN * (2 ** (fail_count - 1)))
    return now + timedelta(minutes=minutes)


def _stats_backoff_active() -> bool:
    """评论/点赞两个目标域名是否处于限流退避期（复用 ithome 的进程内退避表）。"""
    now_ts = time.time()
    return any(_FETCH_BACKOFF.get(h, 0) > now_ts for h in _STATS_HOSTS)


def _get_parsed(url: str, parser) -> tuple[int | None, str]:
    """抓取并解析单个接口。返回 (数值|None, 失败时的响应片段)。

    互动端点是小响应，8s 超时足够；缩短它以压低最坏轮次时长（须 < 锁 TTL）。"""
    resp = _fetch(url, timeout=8)
    if resp is None:
        return None, "fetch failed/backoff"
    if resp.status_code != 200:
        return None, "HTTP %s" % resp.status_code
    text = _decode_response(resp)
    val = parser(text)
    if val is None:
        return None, text[:200]
    return val, ""


def poll_due_articles(db: Session, now: datetime | None = None) -> dict:
    """轮询一批到期文章的互动数据并入库（单篇提交，崩溃不丢已处理进度）。

    到期 = stats_next_poll_at <= now（正常调度）
         或 从未轮询且无排期且未放弃（存量回填/新列首启）。
    """
    now = now or datetime.now(timezone.utc)
    try:
        # 面板热调优先（ALLOWED_KEYS），读不到回落 .env 默认
        from app.services import config_service

        batch = int(config_service.get_effective(db, "stats_poll_batch"))
    except Exception:
        batch = get_settings().stats_poll_batch
    batch = min(max(1, int(batch)), _MAX_BATCH)  # 防面板误设：既不为 0 也不得突破锁 TTL
    due = (
        db.query(Article)
        .filter(
            or_(
                and_(Article.stats_next_poll_at.isnot(None), Article.stats_next_poll_at <= now),
                and_(
                    Article.stats_polled_at.is_(None),
                    Article.stats_next_poll_at.is_(None),
                    Article.stats_fail_count < GIVE_UP_FAILS,
                ),
            )
        )
        .order_by(Article.publish_time.desc())  # 越新的文章互动变化越快，优先服务
        .limit(batch)
        .all()
    )
    logger.info("stats poll round: due=%s batch_limit=%s", len(due), batch)

    polled = ok = failed = gave_up = no_nid = 0
    aborted_backoff = False

    for article in due:
        if not article.news_id:
            # URL 不含文章 ID（理论不该出现）：标记放弃终态，避免每轮重复扫描
            no_nid += 1
            article.stats_fail_count = GIVE_UP_FAILS
            article.stats_polled_at = now
            article.stats_next_poll_at = None
            db.commit()
            logger.warning("stats skip no news_id: id=%s url=%s", article.id, article.url)
            continue
        if _stats_backoff_active():
            aborted_backoff = True
            logger.warning("stats round aborted: target host in backoff（剩余文章保持到期待下轮）")
            break

        comments_new, count_err = _get_parsed(
            _COUNT_URL.format(nid=article.news_id), parse_comment_count
        )
        # 每次请求之间都留间隔（含跨文章的 grade→下一次 count），持续限速防 403
        time.sleep(_POLITENESS_SLEEP_SECONDS)
        likes_new, grade_err = _get_parsed(_GRADE_URL.format(nid=article.news_id), parse_grade_likes)
        time.sleep(_POLITENESS_SLEEP_SECONDS)

        if comments_new is None and likes_new is None:
            # 两个接口都失败：退避重试，已有数据一字不动
            article.stats_fail_count = (article.stats_fail_count or 0) + 1
            nxt = next_poll_after_failure(article.stats_fail_count, now)
            article.stats_next_poll_at = nxt
            failed += 1
            if nxt is None:
                gave_up += 1
                logger.warning(
                    "stats give up id=%s newsid=%s after %d consecutive fails（count: %s / grade: %s）",
                    article.id, article.news_id, article.stats_fail_count, count_err[:80], grade_err[:80],
                )
            else:
                logger.warning(
                    "stats both endpoints failed id=%s newsid=%s fail_no=%s（count: %s / grade: %s）",
                    article.id, article.news_id, article.stats_fail_count, count_err[:80], grade_err[:80],
                )
            db.commit()
            continue

        if comments_new is None:
            logger.warning("comment count parse failed newsid=%s: %s", article.news_id, count_err)
        else:
            old = article.comments or 0
            if old and comments_new < old * 0.7:
                logger.info(
                    "comments dropped %s → %s newsid=%s（删评/审核，接受站点值）",
                    old, comments_new, article.news_id,
                )
            article.comments = comments_new
        if likes_new is None:
            logger.warning("grade likes parse failed newsid=%s: %s", article.news_id, grade_err)
        else:
            article.likes = likes_new

        article.stats_fail_count = 0
        article.stats_polled_at = now
        article.stats_next_poll_at = next_poll_after(article.publish_time, now)
        ok += 1
        db.commit()

    polled = ok + failed + no_nid
    logger.info(
        "stats poll done: due=%s polled=%s ok=%s failed=%s gave_up=%s no_nid=%s aborted_backoff=%s",
        len(due), polled, ok, failed, gave_up, no_nid, aborted_backoff,
    )
    return {
        "due": len(due),
        "polled": polled,
        "ok": ok,
        "failed": failed,
        "gave_up": gave_up,
        "no_nid": no_nid,
        "aborted_backoff": aborted_backoff,
    }
