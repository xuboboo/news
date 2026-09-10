"""互动数据轮询单测：快科技 action.ashx JSONP 解析 / 来源过滤范围 / 分来源调度 / 分层排期。

JSONP 样本取自 2026-09-10 实抓（count.mydrivers.com/news/action.ashx）；
调度测试用 SQLite 内存库 + 打桩 _get_parsed，不触网。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crawler import stats as stats_mod
from app.crawler.stats import (
    GIVE_UP_FAILS,
    next_poll_after,
    next_poll_after_failure,
    parse_comment_count,
    parse_grade_likes,
    parse_kj_action_stats,
    poll_due_articles,
    stats_source_filter,
)
from app.models.article import Article


# ———————————————————— 快科技 action.ashx JSONP 解析 ————————————————————

def test_parse_kj_action_stats_full():
    # 2026-09-10 实抓样本（newsid 1149642）：展示评论数 = rcount + rcanyu = 108
    text = 'news_info({"goodnums":15,"rcount":8,"rcanyu":100,"share":2})'
    assert parse_kj_action_stats(text) == (108, 15)


def test_parse_kj_action_stats_empty_json_is_zero_not_failure():
    # 零互动文章站点返回空对象（实抓 1149200）：有效响应全 0，不能当失败——
    # 否则会被失败退避反复重试直至放弃终态
    assert parse_kj_action_stats("news_info({})") == (0, 0)


def test_parse_kj_action_stats_missing_fields_default_zero():
    assert parse_kj_action_stats('news_info({"rcount":3})') == (3, 0)


def test_parse_kj_action_stats_negative_clamped():
    # 负值逐字段截 0（rcount=-5→0，rcanyu=2 保留）：和 = 2
    assert parse_kj_action_stats('news_info({"rcount":-5,"rcanyu":2,"goodnums":-1})') == (2, 0)


def test_parse_kj_action_stats_garbage_returns_none():
    assert parse_kj_action_stats("") is None
    assert parse_kj_action_stats("Object moved") is None
    assert parse_kj_action_stats("news_info(oops)") is None
    assert parse_kj_action_stats("news_info([1,2])") is None


def test_parse_kj_action_stats_non_numeric_values():
    assert parse_kj_action_stats('news_info({"goodnums":"abc","rcount":null})') == (0, 0)


# ———————————————————— 既有 IT之家解析器回归 ————————————————————

def test_parse_comment_count():
    assert parse_comment_count("ccd.innerHTML = '226'") == 226


def test_parse_grade_likes():
    html = '<span class="s" id="sgrade2"><a>有价值</a><br><div>68</div></span>'
    assert parse_grade_likes(html) == 68


# ———————————————————— 来源过滤范围与排期 ————————————————————

def test_stats_source_filter_covers_both_sources():
    compiled = str(stats_source_filter().compile(compile_kwargs={"literal_binds": True}))
    assert "IT之家" in compiled
    assert "快科技" in compiled


def test_next_poll_after_tiers_and_window():
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(minutes=30)
    nxt = next_poll_after(fresh, now)
    assert nxt is not None and timedelta(minutes=29) <= nxt - now <= timedelta(minutes=31)
    # 超出 14 天停采窗口 → 终态 None
    assert next_poll_after(now - timedelta(days=15), now) is None


def test_next_poll_after_failure_backoff_and_give_up():
    now = datetime.now(timezone.utc)
    assert next_poll_after_failure(1, now) - now == timedelta(minutes=15)
    assert next_poll_after_failure(GIVE_UP_FAILS, now) is None


# ———————————————————— poll_due_articles 分来源调度（SQLite + 打桩） ————————————————————

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Article.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _mk_article(db, source, news_id, hours_old=1.0, **kw):
    fields = dict(views=0, comments=0, shares=0, likes=0)
    fields.update(kw)
    art = Article(
        title="t",
        url=f"https://example.com/{source}/{news_id}",
        channel="c",
        source=source,
        news_id=news_id,
        content="body",
        publish_time=datetime.now(timezone.utc) - timedelta(hours=hours_old),
        ai_status="pending",
        **fields,
    )
    db.add(art)
    db.commit()
    return art


def _stub_no_network(monkeypatch, respond):
    """respond(url) -> (解析后的值 | None, err)；打桩网络层（解析器已被上方单测覆盖），
    顺带关掉轮内限速 sleep。"""
    monkeypatch.setattr(stats_mod, "_get_parsed", lambda url, parser: respond(url))
    monkeypatch.setattr(stats_mod.time, "sleep", lambda s: None)


def test_poll_kj_single_endpoint_fills_comments_and_likes(db, monkeypatch):
    art = _mk_article(db, "快科技", "1149642")

    def respond(url):
        assert "action.ashx?tid=1149642" in url
        return ((108, 15), "")

    _stub_no_network(monkeypatch, respond)
    result = poll_due_articles(db)
    assert result["ok"] == 1 and result["failed"] == 0

    db.refresh(art)
    assert art.comments == 108
    assert art.likes == 15
    assert art.stats_fail_count == 0
    assert art.stats_polled_at is not None
    # 新文处于 0-2h 黄金档：下次排期约 30 分钟后
    assert art.stats_next_poll_at is not None
    assert art.stats_next_poll_at > art.stats_polled_at


def test_poll_kj_parse_failure_backs_off_without_data_overwrite(db, monkeypatch):
    art = _mk_article(db, "快科技", "42", comments=7, likes=3)

    _stub_no_network(monkeypatch, lambda url: (None, "Object moved"))

    result = poll_due_articles(db)
    assert result["failed"] == 1 and result["ok"] == 0
    db.refresh(art)
    # 已有数据一字不动，失败退避 15min
    assert art.comments == 7 and art.likes == 3
    assert art.stats_fail_count == 1
    assert art.stats_next_poll_at is not None


def test_poll_dispatches_both_sources_in_one_round(db, monkeypatch):
    kj = _mk_article(db, "快科技", "1001")
    it = _mk_article(db, "IT之家", "1002")

    def respond(url):
        if "action.ashx" in url:
            return ((7, 9), "")
        if "comment/count" in url:
            return (226, "")
        assert "grade" in url
        return (68, "")

    _stub_no_network(monkeypatch, respond)
    result = poll_due_articles(db)
    assert result["ok"] == 2

    db.refresh(kj)
    db.refresh(it)
    assert (kj.comments, kj.likes) == (7, 9)
    assert (it.comments, it.likes) == (226, 68)


def test_poll_source_backoff_skips_only_that_source(db, monkeypatch):
    kj = _mk_article(db, "快科技", "2001")
    it = _mk_article(db, "IT之家", "2002")
    # 快科技接口域名限流中
    stats_mod._FETCH_BACKOFF["count.mydrivers.com"] = datetime.now(timezone.utc).timestamp() + 600

    polled_urls: list[str] = []

    def respond(url):
        polled_urls.append(url)
        if "comment/count" in url:
            return (11, "")
        assert "grade" in url
        return (22, "")

    try:
        _stub_no_network(monkeypatch, respond)
        result = poll_due_articles(db)
    finally:
        stats_mod._FETCH_BACKOFF.pop("count.mydrivers.com", None)

    # 快科技被跳过（保持到期），IT之家正常轮询不被饿死
    assert result["ok"] == 1 and result["aborted_backoff"] is True
    assert polled_urls and all("action.ashx" not in u for u in polled_urls)
    db.refresh(kj)
    db.refresh(it)
    assert kj.comments == 0 and kj.stats_polled_at is None
    assert (it.comments, it.likes) == (11, 22)


def test_poll_ignores_sources_outside_scope(db, monkeypatch):
    _mk_article(db, "未知来源", "3001")

    _stub_no_network(monkeypatch, lambda url: (None, "should not be called"))
    result = poll_due_articles(db)
    assert result["due"] == 0 and result["polled"] == 0


def test_poll_first_time_backfill_beats_reschedule_queue(db, monkeypatch):
    """首采优先于复采：从未轮询的旧文必须先于「到期复采」的较新文拿到首份数据。

    复采瀑布（轮询后 +30min~12h 重排、永远占着新鲜排）在 publish_time 倒序下会
    长期饿死回填尾（停机积压/新来源接入），本测试锁定两段排序语义。
    """
    _mk_article(db, "快科技", "4001", hours_old=24 * 10)  # 从未轮询（回填桶）
    fresh_it = _mk_article(db, "IT之家", "4002", hours_old=1)
    fresh_it.stats_polled_at = datetime.now(timezone.utc) - timedelta(hours=2)
    fresh_it.stats_next_poll_at = datetime.now(timezone.utc) - timedelta(minutes=5)  # 到期复采
    db.commit()

    # 批次压到 1：一轮只服务一篇文章
    class _S:
        stats_poll_batch = 1

    monkeypatch.setattr(stats_mod, "get_settings", _S)

    polled: list[str] = []

    def respond(url):
        polled.append(url)
        return ((5, 5), "") if "action.ashx" in url else (7, "")

    _stub_no_network(monkeypatch, respond)

    result = poll_due_articles(db)
    assert result["ok"] == 1
    assert any("action.ashx?tid=4001" in u for u in polled), f"首采被饿死: {polled}"
    db.refresh(fresh_it)
    assert fresh_it.comments == 0  # 复采文本轮未被服务
