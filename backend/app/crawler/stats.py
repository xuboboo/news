"""多来源互动数据轮询：评论数 + 点赞数（热度算法的真实互动输入）。

数据源（2026-09 实测可用，均为站点页面自加载的接口，无需登录/签名）：

IT之家：
- 评论数：GET https://cmt.ithome.com/api/comment/count?newsid=<id>
  返回 JS 片段 `ccd.innerHTML = '226'`
- 点赞（文章价值打分中的「有价值」数）：GET https://dyn.ithome.com/grade/<newsid>
  返回 JS 片段，含 价值分 / 打分人数 / 有价值 / 无价值
- 阅读数接口（dyn.ithome.com/api/hitcount/<id>/pc）GET/POST 均返回 200 空 body
  （只写计数器），无数据源，views 保持 0。

快科技：
- 单接口 GET https://count.mydrivers.com/news/action.ashx?tid=<id>
  返回 JSONP `news_info({"goodnums":15,"rcount":8,"rcanyu":100,"share":2})`：
  goodnums=点赞（与 NewsVoteForJsonp.aspx 的 NewsSupport 同值），站点展示评论数
  = rcount + rcanyu（文章页 initnews_info() 的展示口径），一次请求双指标齐。
- 阅读/分享不启用（HitsUTF8.aspx 的 hits 与 share 字段）：IT之家两侧均无数据源，
  单边启用会让快科技在 Engagement 上获得系统性虚高（量纲不对称），views/shares
  维持 0，留分来源归一化专项。

调度模型（分层衰减节奏）：文章越新采越勤、越旧越疏，超采集窗口停采。
状态语义见 Article.stats_* 字段注释；失败绝不覆写已有数据（接口各自独立，
哪个成功更新哪个，防止把真实互动数冲掉）。

运维注意：进程内退避表与 lock:stats 互斥锁都以「worker/crawler 单进程持有」为前提，
排查时勿用 docker exec 直调 poll_due_articles 绕锁（冷退避表会重复打已限流的域名、
并与 beat 轮次形成双跑；数据幂等无害但浪费请求配额）。手动触发走
POST /api/v2/admin/stats 或等 beat 15 分钟节奏。

合规态势（2026-09-11 实测记录）：count/dt.mydrivers.com 与 cmt/dyn.ithome.com
四个互动接口域名的 robots.txt 均为 Disallow: /。本模块维持轮询的依据：这些端点
是文章页对匿名浏览器自加载的公开计数接口，请求与访客浏览器逐字节对等（同端点、
同 JSONP 响应、只读 GET、无鉴权绕过）；量级有硬上界（分层节奏 + 14 天停采窗 +
单批 ≤50，单来源日请求量数百次级，远低于任一真实访客的浏览量）；指数退避与
403 熔断已内置。若站点侧出现 403 升级/措辞警告，熔断路径 = 面板热停
stats_poll_enabled 或下线对应来源，勿绕过限流硬闯。
"""
import json
import logging
import math
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

_KJ_ACTION_URL = "https://count.mydrivers.com/news/action.ashx?tid={nid}"
_KJ_STATS_HOSTS = ("count.mydrivers.com",)

# 来源 → 该来源互动接口的域名集（退避判断按来源分流，单源限流不饿死另一源）
_SOURCE_STATS_HOSTS = {
    "IT之家": _STATS_HOSTS,
    "快科技": _KJ_STATS_HOSTS,
}

# 评论数 JS：专属正则优先，宽松回退兜底格式微调（回退锚定 ccd 所在行，防捕获页面无关数字）
_COUNT_RE = re.compile(r"ccd\.innerHTML\s*=\s*'(\d+)'")
_COUNT_RE_FALLBACK = re.compile(r"ccd[^\n]*innerHTML\s*=\s*'(\d+)'")
# 点赞 JS：`<span class="s" id="sgrade2"><a>有价值</a><br><div>68</div></span>`
_GRADE_LIKE_RE = re.compile(r'id="sgrade2">\s*<a>有价值</a>\s*<br>\s*<div>(\d+)</div>')

# 快科技 action.ashx：JSONP 包裹的 JSON 对象，回调名固定 news_info（不带 callback 参数
# 时端点也返回该默认名，生产实抓确认）。锚定前缀防止 HTML 错误页/JSON-LD 等无关
# 花括号块被误解析成"合法零值"清掉真实互动数据
_KJ_ACTION_JSON_RE = re.compile(r"news_info\(\s*(\{[^{}]*\})\s*\)")
# 判定"有效业务响应"的键：非空 dict 至少含其一，否则视为错误体（如 {"errcode":-1}）
_KJ_KNOWN_KEYS = {"rcount", "rcanyu", "goodnums"}

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
# 单轮批次上限：httpx 标量 timeout 按阶段计（connect/read 各 8s），单请求最坏 ~16s，
# 单篇双端点+间隔最坏 ~33s → 取 50（50×33s ≈ 28min），与下方轮次硬截止双保险 < 锁 TTL 30min
_MAX_BATCH = 50
_ROUND_DEADLINE_SECONDS = 25 * 60  # 单轮硬截止：重定向链等最坏耗时无法精确估算，超时收尾保锁不失效


def _clamp_count(v: str) -> int:
    """互动数值统一钳制：防端点异常返回 >2^63 大整数在 BigInteger 落库时炸整轮
    （IT之家与快科技两侧行为对称；正则只捕获 \\d+，int 不会失败）。"""
    return max(0, min(int(v), 10**12))


def parse_comment_count(text: str) -> int | None:
    m = _COUNT_RE.search(text or "") or _COUNT_RE_FALLBACK.search(text or "")
    return _clamp_count(m.group(1)) if m else None


def parse_kj_action_stats(text: str) -> tuple[int, int] | None:
    """快科技 action.ashx 响应 → (评论数, 点赞数)；无 JSON 体/格式非法返回 None。

    评论数 = rcount + rcanyu（站点文章页 initnews_info() 的展示口径，与用户所见一致）；
    点赞 = goodnums。字段缺失按 0 计（站点 JS 同款语义），负值防御性截 0；
    空 JSON `{}` 也算有效响应（该文确实零互动），不能当失败——否则零互动文章
    会被失败退避反复重试直至放弃终态。非空 dict 必须至少含一个已知业务键，
    否则按错误体拒收（防 {"errcode":-1} 之类静默归零真实数据）。
    """
    m = _KJ_ACTION_JSON_RE.search(text or "")
    if m is None:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data and not (_KJ_KNOWN_KEYS & data.keys()):
        return None
    # 非有限浮点（1e999/Infinity 解析为 inf）：整包按毒响应拒收——若只把该字段归 0，
    # 会以"成功"身份静默清掉真实互动，与钳制保留量级的语义相悖
    for key in _KJ_KNOWN_KEYS:
        val = data.get(key)
        if isinstance(val, float) and not math.isfinite(val):
            return None

    def _int(key: str) -> int:
        try:
            # 钳制上限防 >2^63 大整数在 PostgreSQL BigInteger 落库时炸整轮
            return max(0, min(int(data.get(key) or 0), 10**12))
        except (TypeError, ValueError):
            return 0

    comments = _int("rcount") + _int("rcanyu")
    return comments, _int("goodnums")


def stats_source_filter():
    """互动轮询的范围条件：评论/点赞接口已覆盖的全部来源（IT之家 + 快科技）。

    poll_due_articles 与 admin.py 的轮询水位统计共用此条件，防止两处口径漂移
    （漏加会让不排期轮询的来源文章永久计入「待回填」）。views/shares 不在
    轮询范围（见模块 docstring 的量纲说明），但那两列本就不依赖轮询排期。
    """
    return Article.source.in_(tuple(_SOURCE_STATS_HOSTS))


def parse_grade_likes(text: str) -> int | None:
    m = _GRADE_LIKE_RE.search(text or "")
    return _clamp_count(m.group(1)) if m else None


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


def _backed_off_sources() -> set[str]:
    """当前处于限流退避期的来源集合（按来源域名集判定，复用 ithome 的进程内退避表）。"""
    now_ts = time.time()
    return {
        source
        for source, hosts in _SOURCE_STATS_HOSTS.items()
        if any(_FETCH_BACKOFF.get(h, 0) > now_ts for h in hosts)
    }


def _get_parsed(url: str, parser) -> tuple[object | None, str]:
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


def _poll_article(article: Article) -> tuple[int | None, int | None, str, str]:
    """按来源轮询单篇互动数据 → (comments|None, likes|None, count_err, grade_err)。

    快科技单接口双指标，count/grade 两个错误位填同一个错误（供失败日志共用格式）。
    """
    if article.source == "快科技":
        stats, err = _get_parsed(_KJ_ACTION_URL.format(nid=article.news_id), parse_kj_action_stats)
        time.sleep(_POLITENESS_SLEEP_SECONDS)
        if stats is None:
            return None, None, err, err
        return stats[0], stats[1], "", ""

    comments, count_err = _get_parsed(
        _COUNT_URL.format(nid=article.news_id), parse_comment_count
    )
    # 每次请求之间都留间隔（含跨文章的 grade→下一次 count），持续限速防 403
    time.sleep(_POLITENESS_SLEEP_SECONDS)
    likes, grade_err = _get_parsed(_GRADE_URL.format(nid=article.news_id), parse_grade_likes)
    time.sleep(_POLITENESS_SLEEP_SECONDS)
    return comments, likes, count_err, grade_err


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
    # 退避来源整体排除在 LIMIT 之外：若只在 Python 侧逐篇跳过，被跳过的文章仍占满
    # 批次名额，批间饿死健康来源（与"复采瀑布饿死回填尾"同构，只是触发面换成退避）
    backed_off = _backed_off_sources()
    range_conditions = [stats_source_filter()]
    if backed_off:
        range_conditions.append(Article.source.notin_(tuple(backed_off)))
    due = (
        db.query(Article)
        .filter(
            # 范围 = stats_source_filter()（IT之家 + 快科技）；views/shares 不排期无妨，
            # 那两列不依赖轮询。范围外的来源（未来新源）必须在此挡住，否则会被下方
            # "从未轮询"回填分支捞走打错误接口连败 6 次，且挤占批次饿死已接入来源
            *range_conditions,
            or_(
                and_(Article.stats_next_poll_at.isnot(None), Article.stats_next_poll_at <= now),
                and_(
                    Article.stats_polled_at.is_(None),
                    Article.stats_next_poll_at.is_(None),
                    Article.stats_fail_count < GIVE_UP_FAILS,
                ),
            ),
        )
        # 首采优先于复采（两段排序）：从未轮询的文章（新来源接入回填/停机积压）先拿第一份
        # 数据——否则 publish_time 倒序下，复采瀑布（到期即重排 +30min~12h，永远占着新鲜
        # 排）会长期饿死排在其后的回填尾；各类内部仍按"越新互动变化越快"优先
        .order_by(
            Article.stats_polled_at.is_(None).desc(),
            Article.publish_time.desc(),
        )
        .limit(batch)
        .all()
    )
    logger.info(
        "stats poll round: due=%s batch_limit=%s backed_off_sources=%s",
        len(due), batch, sorted(backed_off) or "none",
    )

    polled = ok = failed = gave_up = no_nid = 0
    skipped_backoff = 0
    aborted_backoff = False
    deadline_hit = False
    # 轮次硬截止（< 锁 TTL 30min）：重定向链/慢响应的最坏耗时无法精确估算，
    # 超时即收尾，剩余文章保持到期下轮再试——锁 TTL 从"估算约束"变"硬约束"
    deadline = time.monotonic() + _ROUND_DEADLINE_SECONDS

    for article in due:
        if not article.news_id:
            # URL 不含文章 ID（理论不该出现）：标记放弃终态，避免每轮重复扫描。
            # 不写 stats_polled_at：它语义是"成功轮询过"，误写会污染 admin「近24h成功」口径；
            # fail_count 达 GIVE_UP 后回填分支与 due 条件天然排除，无需其他标记
            no_nid += 1
            article.stats_fail_count = GIVE_UP_FAILS
            article.stats_next_poll_at = None
            db.commit()
            logger.warning("stats skip no news_id: id=%s url=%s", article.id, article.url)
            continue
        if article.source in _backed_off_sources():
            # 轮中新增的退避（查询后才触发）兜底跳过：正常情况已在 SQL 侧整源排除
            skipped_backoff += 1
            aborted_backoff = True
            logger.info("stats skip in-backoff host: id=%s source=%s", article.id, article.source)
            continue
        if time.monotonic() > deadline:
            deadline_hit = True
            # polled 此刻已含本轮已处理数（ok+failed+no_nid 实时累计），剩余=总-已处理
            logger.warning(
                "stats round deadline (%ds) hit, %s due articles left for next round",
                _ROUND_DEADLINE_SECONDS, len(due) - (ok + failed + no_nid),
            )
            break

        try:
            comments_new, likes_new, count_err, grade_err = _poll_article(article)
        except Exception as exc:
            # 单篇意外异常（解析层已兜 OverflowError 等，此处最后防线）：
            # 并入下方失败路径退避，绝不让单篇毒响应带崩整轮
            logger.exception(
                "stats poll unexpected error id=%s newsid=%s", article.id, article.news_id
            )
            comments_new = likes_new = None
            count_err = grade_err = "unexpected: %s" % exc

        if comments_new is None and likes_new is None:
            # 该篇全部接口失败：退避重试，已有数据一字不动
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
                    "stats all endpoints failed id=%s newsid=%s fail_no=%s（count: %s / grade: %s）",
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
        try:
            db.commit()
        except Exception:
            # 落库异常的残余场景（如数值超列宽的未知路径）：回滚后该篇回到处理前
            # 状态（保持到期，下轮再试），绝不让单篇落库失败带崩整轮
            db.rollback()
            logger.exception(
                "stats commit failed id=%s newsid=%s（已回滚，保持到期）",
                article.id, article.news_id,
            )
            continue
        ok += 1

    polled = ok + failed + no_nid
    logger.info(
        "stats poll done: due=%s polled=%s ok=%s failed=%s gave_up=%s no_nid=%s "
        "skipped_backoff=%s deadline_hit=%s",
        len(due), polled, ok, failed, gave_up, no_nid, skipped_backoff, deadline_hit,
    )
    return {
        "due": len(due),
        "polled": polled,
        "ok": ok,
        "failed": failed,
        "gave_up": gave_up,
        "no_nid": no_nid,
        "skipped_backoff": skipped_backoff,
        "aborted_backoff": aborted_backoff,
        "deadline_hit": deadline_hit,
    }
