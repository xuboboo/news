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


def test_parse_kj_action_stats_requires_jsonp_anchor():
    # HTML 错误页/页面里的 JSON-LD 等无关花括号块：没有 news_info( 前缀一律拒收，
    # 防止被误判成"合法零互动"清掉真实数据
    assert parse_kj_action_stats('{"rcount":999,"goodnums":999}') is None
    assert parse_kj_action_stats('<script>{"@context":"https://schema.org","x":1}</script>') is None
    assert parse_kj_action_stats('other_cb({"rcount":5,"goodnums":2})') is None


def test_parse_kj_action_stats_error_object_rejected():
    # 非空 dict 但不含任何已知业务键：按错误体拒收（走失败退避），不得静默归零
    assert parse_kj_action_stats('news_info({"errcode":-1,"msg":"forbidden"})') is None


def test_parse_kj_action_stats_infinity_and_huge_numbers():
    # JSON 的 1e999/Infinity 解析为 inf：整包按毒响应拒收（返回 None 走失败退避）——
    # 若只把该字段归 0，会以"成功"身份静默清掉真实互动
    assert parse_kj_action_stats('news_info({"rcount":1e999,"goodnums":Infinity})') is None
    assert parse_kj_action_stats('news_info({"rcount":-Infinity,"goodnums":1})') is None
    # > 2^63 大整数钳制到 10^12，防 PostgreSQL BigInteger 落库炸整轮
    assert parse_kj_action_stats('news_info({"rcount":1,"rcanyu":0,"goodnums":99999999999999999})') == (1, 10**12)


def test_parse_kj_action_stats_string_numbers_ok():
    assert parse_kj_action_stats('news_info({"rcount":"8","rcanyu":"2","goodnums":"15"})') == (10, 15)


# ———————————————————— 既有 IT之家解析器回归 ————————————————————

def test_parse_comment_count():
    assert parse_comment_count("ccd.innerHTML = '226'") == 226


def test_parse_comment_count_huge_number_clamped():
    # 端点异常返回 >2^63 大整数：钳到 10^12，防 BigInteger 落库炸整轮（与 KJ 侧对称）
    assert parse_comment_count("ccd.innerHTML = '99999999999999999999'") == 10**12


def test_parse_grade_likes():
    html = '<span class="s" id="sgrade2"><a>有价值</a><br><div>68</div></span>'
    assert parse_grade_likes(html) == 68
    assert parse_grade_likes('id="sgrade2"><a>有价值</a><br><div>99999999999999999999</div>') == 10**12


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


def test_poll_source_backoff_excluded_from_batch(db, monkeypatch):
    """退避来源在 SQL 侧整体排除：不占批次名额，健康来源不被批间饿死。"""
    kj = _mk_article(db, "快科技", "2001")  # 排期参数已不影响结果（整源不进 due）
    it = _mk_article(db, "IT之家", "2002", hours_old=2)
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

    # 快科技被整源排除（due 里根本没有它），IT之家正常轮询
    assert result["due"] == 1 and result["ok"] == 1
    assert result["skipped_backoff"] == 0 and result["aborted_backoff"] is False
    assert polled_urls and all("action.ashx" not in u for u in polled_urls)
    db.refresh(kj)
    db.refresh(it)
    assert kj.comments == 0 and kj.stats_polled_at is None
    assert (it.comments, it.likes) == (11, 22)


def test_poll_poison_article_does_not_kill_round(db, monkeypatch):
    """单篇意外异常（如毒响应）按失败退避并入批次，绝不带崩整轮。"""
    poison = _mk_article(db, "快科技", "5001", hours_old=1)
    healthy = _mk_article(db, "IT之家", "5002", hours_old=2)

    def respond(url):
        if "action.ashx" in url:
            raise RuntimeError("boom: simulated poison response")
        if "comment/count" in url:
            return (33, "")
        assert "grade" in url
        return (44, "")

    _stub_no_network(monkeypatch, respond)
    result = poll_due_articles(db)
    assert result["ok"] == 1 and result["failed"] == 1

    db.refresh(poison)
    db.refresh(healthy)
    assert poison.stats_fail_count == 1 and poison.stats_next_poll_at is not None
    assert poison.comments == 0  # 已有数据一字不动
    assert (healthy.comments, healthy.likes) == (33, 44)


def test_poll_mixed_success_and_failure_independent(db, monkeypatch):
    fail_kj = _mk_article(db, "快科技", "6001", hours_old=1, comments=9, likes=8)
    ok_it = _mk_article(db, "IT之家", "6002", hours_old=2)

    def respond(url):
        if "action.ashx" in url:
            return (None, "Object moved")
        if "comment/count" in url:
            return (7, "")
        assert "grade" in url
        return (0, "")

    _stub_no_network(monkeypatch, respond)
    result = poll_due_articles(db)
    assert (result["ok"], result["failed"]) == (1, 1)

    db.refresh(fail_kj)
    db.refresh(ok_it)
    # 失败侧数据与计数独立：不覆写、退避排期
    assert (fail_kj.comments, fail_kj.likes, fail_kj.stats_fail_count) == (9, 8, 1)
    # 成功侧即使点赞为 0 也算成功（合法零值）
    assert (ok_it.comments, ok_it.likes, ok_it.stats_fail_count) == (7, 0, 0)


def test_poll_no_nid_terminal_without_polluting_polled_stamp(db, monkeypatch):
    """news_id 缺失 → 放弃终态，但不写 stats_polled_at（它语义=成功轮询过，
    误写会污染 admin「近24h成功轮询」水位口径）。"""
    bad = Article(
        title="t",
        url="https://example.com/no-nid",
        channel="c",
        source="快科技",
        news_id=None,
        content="body",
        publish_time=datetime.now(timezone.utc) - timedelta(hours=1),
        views=0,
        comments=0,
        shares=0,
        likes=0,
        ai_status="pending",
    )
    db.add(bad)
    db.commit()

    _stub_no_network(monkeypatch, lambda url: (None, "must not be called"))
    first = poll_due_articles(db)
    assert first["no_nid"] == 1 and first["polled"] == 1

    db.refresh(bad)
    assert bad.stats_fail_count == GIVE_UP_FAILS
    assert bad.stats_polled_at is None
    assert bad.stats_next_poll_at is None

    # 终态收敛：下一轮不再被回填分支捞起
    second = poll_due_articles(db)
    assert second["due"] == 0 and second["polled"] == 0


def test_poll_round_deadline_breaks_and_keeps_articles_due(db, monkeypatch):
    """25min 硬截止触发：立即收尾，未处理文章保持到期，deadline_hit 如实上报。"""
    _mk_article(db, "快科技", "7001")
    _mk_article(db, "快科技", "7002")

    # 截止时刻调到过去（-1）：循环第一篇即触达（Windows monotonic 分辨率粗，0 可能相等）
    monkeypatch.setattr(stats_mod, "_ROUND_DEADLINE_SECONDS", -1)

    result = poll_due_articles(db)
    assert result["due"] == 2
    assert result["polled"] == 0 and result["ok"] == 0
    assert result["deadline_hit"] is True

    # 未处理文章状态一字未动：仍然到期，下轮再试
    arts = db.query(Article).filter(Article.source == "快科技").all()
    assert all(a.stats_polled_at is None for a in arts)
    assert all(a.stats_next_poll_at is None for a in arts)  # 回填桶原样保留


def test_poll_midround_backoff_skip_counts_and_keeps_due(db, monkeypatch):
    """轮中新增退避的批内兜底：同源后续篇被跳过并计入 skipped_backoff，保持到期。"""
    first = _mk_article(db, "快科技", "8001", hours_old=1)  # publish 较新 → 先被服务
    second = _mk_article(db, "快科技", "8002", hours_old=2)

    def respond(url):
        if "action.ashx?tid=8001" in url:
            # 首篇轮询途中快科技域名进入退避（模拟轮中新增）
            stats_mod._FETCH_BACKOFF["count.mydrivers.com"] = (
                datetime.now(timezone.utc).timestamp() + 600
            )
            return ((5, 5), "")
        return ((6, 6), "")  # 兼作绊线：若 8002 被轮询则断言必失败

    try:
        _stub_no_network(monkeypatch, respond)
        result = poll_due_articles(db)
    finally:
        stats_mod._FETCH_BACKOFF.pop("count.mydrivers.com", None)

    assert result["ok"] == 1
    assert result["skipped_backoff"] == 1 and result["aborted_backoff"] is True

    db.refresh(first)
    db.refresh(second)
    assert (first.comments, first.likes) == (5, 5)
    assert second.comments == 0 and second.stats_polled_at is None  # 跳过且保持到期


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


# ———————————————————— 放弃终态自愈（2026-09-13 审计） ————————————————————

def _mk_gave_up(db, news_id, hours_old):
    """直接构造放弃终态文章（fail_count 打满 + 无排期），模拟站点故障期间的 6 连败。"""
    return _mk_article(
        db, "快科技", news_id, hours_old=hours_old, stats_fail_count=GIVE_UP_FAILS
    )


def _ok_respond():
    def respond(url):
        if "action.ashx" in url:
            return ((5, 5), "")
        if "comment/count" in url:
            return (7, "")
        return (11, "")

    return respond


def test_heal_requeues_gave_up_on_healthy_round(db, monkeypatch):
    """健康轮次（ok>=3 且 failed==0）后：窗口内的放弃文章被重排回待采。"""
    gave_up = _mk_gave_up(db, "9001", hours_old=5)
    for i in range(3):
        _mk_article(db, "IT之家", f"910{i}")  # 3 篇必成功，凑健康轮次

    _stub_no_network(monkeypatch, _ok_respond())
    result = poll_due_articles(db)
    assert (result["ok"], result["failed"]) == (3, 0)
    assert result["healed"] == 1

    db.refresh(gave_up)
    assert gave_up.stats_fail_count == 0
    assert gave_up.stats_next_poll_at is not None
    assert gave_up.stats_polled_at is None  # 重排不写成功戳（语义=成功轮询过）


def test_heal_blocked_when_round_has_failure(db, monkeypatch):
    """轮内有失败（站点可能仍在故障）：即使 ok 达标也不重排，防空耗配额。"""
    gave_up = _mk_gave_up(db, "9201", hours_old=5)
    _mk_article(db, "IT之家", "9202")
    _mk_article(db, "IT之家", "9203")
    fail_kj = _mk_article(db, "快科技", "9204")

    def respond(url):
        if "action.ashx?tid=9204" in url:
            return (None, "still 403")
        if "action.ashx" in url:
            return ((5, 5), "")
        if "comment/count" in url:
            return (7, "")
        return (11, "")

    _stub_no_network(monkeypatch, respond)
    result = poll_due_articles(db)
    assert (result["ok"], result["failed"]) == (2, 1)
    assert result["healed"] == 0

    db.refresh(gave_up)
    assert gave_up.stats_fail_count == GIVE_UP_FAILS
    assert gave_up.stats_next_poll_at is None


def test_heal_disabled_when_batch_zero(db, monkeypatch):
    """stats_heal_batch=0（面板关闭自愈）：健康轮次也不重排。"""
    import app.services.config_service as config_service_mod

    gave_up = _mk_gave_up(db, "9401", hours_old=5)
    for i in range(3):
        _mk_article(db, "IT之家", f"941{i}")

    def _fake_get_effective(db_, key):
        if key == "stats_heal_batch":
            return 0
        raise RuntimeError("no system_setting table")  # 其余键走真实回退路径（默认 50）

    monkeypatch.setattr(config_service_mod, "get_effective", _fake_get_effective)
    _stub_no_network(monkeypatch, _ok_respond())
    result = poll_due_articles(db)
    assert (result["ok"], result["failed"]) == (3, 0)
    assert result["healed"] == 0

    db.refresh(gave_up)
    assert gave_up.stats_fail_count == GIVE_UP_FAILS


def test_heal_skips_articles_beyond_stats_window(db, monkeypatch):
    """超 14 天停采窗口的放弃文章不救：重排也会被 next_poll_after 判终态，白耗批次。"""
    stale = _mk_gave_up(db, "9501", hours_old=24 * 15)
    for i in range(3):
        _mk_article(db, "IT之家", f"952{i}")

    _stub_no_network(monkeypatch, _ok_respond())
    result = poll_due_articles(db)
    assert (result["ok"], result["failed"]) == (3, 0)
    assert result["healed"] == 0

    db.refresh(stale)
    assert stale.stats_fail_count == GIVE_UP_FAILS
    assert stale.stats_next_poll_at is None
