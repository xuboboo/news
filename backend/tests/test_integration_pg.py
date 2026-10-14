"""数据库集成测试（2026-09-09 审计补齐：run_event_clustering / 早报窗口 / 热榜窗口）。

需要 postgres + pgvector 的**独立测试库**，未设置 TEST_DATABASE_URL 时整体跳过：
    TEST_DATABASE_URL=postgresql+psycopg://hjradar:hjradar@postgres:5432/hjradar_test \
        python -m pytest tests/test_integration_pg.py -q

容器内运行（推荐，postgres 主机名才可解析）：
    docker compose exec postgres psql -U hjradar -c 'CREATE DATABASE hjradar_test'
    docker compose exec -e TEST_DATABASE_URL=postgresql+psycopg://hjradar:hjradar@postgres:5432/hjradar_test \
        backend python -m pytest tests/test_integration_pg.py -q

事件总结走本地事实拼装（LLM 路径未接线），全程无外部调用；每用例前清空相关表。
"""
import os
from datetime import datetime, timedelta, timezone

from pg_safety import validate_test_database_url

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.article import Article  # noqa: F401  注册 metadata
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport  # noqa: F401
from app.models.report_delivery import ReportDelivery  # noqa: F401
from app.models.setting import SystemSetting  # noqa: F401
from app.models.summary import ArticleSummary  # noqa: F401
from app.models.task_run import TaskRun  # noqa: F401
from app.models.usage import AiUsageLog, ModelPrice  # noqa: F401
from app.utils.tz import TZ_BJ

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="需要 TEST_DATABASE_URL 指向含 pgvector 的 postgres 测试库",
)

TABLES = (
    "article", "article_summary", "event", "event_article", "daily_report", "report_delivery",
    "article_draft",
    "ai_usage_log", "model_price", "system_setting", "task_run",
)

DIM = get_settings().embed_dim  # 与部署环境一致（本地 hash 256 / fastembed 384）
NOW = datetime.now(timezone.utc)


def _vec(direction: list[float]) -> list[float]:
    """按方向分量构造归一化 256 维向量；direction 只给前若干维。"""
    v = list(direction) + [0.0] * (DIM - len(direction))
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def _offset(a: list[float], perp: list[float], cos: float) -> list[float]:
    """精确余弦构造：返回与 a 余弦恰为 cos 的向量（perp 须为单位正交向量）。"""
    sin = (1 - cos * cos) ** 0.5
    v = [cos * x + sin * y for x, y in zip(a, perp)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@pytest.fixture()
def pg():
    """独立测试库会话工厂：在建表、清理前再次执行 fail-closed 校验。"""
    validate_test_database_url(os.environ.get("TEST_DATABASE_URL"))
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    from app.core.db import Base

    Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in TABLES])
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _clean():
        # The guard is intentionally immediately before destructive SQL.
        validate_test_database_url(os.environ.get("TEST_DATABASE_URL"))
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} RESTART IDENTITY CASCADE"))

    _clean()
    yield factory
    _clean()
    engine.dispose()


def _mk_article(db, *, title: str, source: str, vec: list[float], published: datetime,
                channel: str = "智能时代", views: int = 0) -> Article:
    a = Article(
        title=title,
        url=f"https://test.local/{title}-{published.timestamp()}-{source}",
        channel=channel,
        source=source,
        news_id=f"n-{title}-{published.timestamp()}-{source}",
        publish_time=published,
        embedding=vec,
        ai_status="embedded",
        views=views,
    )
    db.add(a)
    db.flush()
    return a


# ———— run_event_clustering：并入已有事件的语义门槛（P0） ————

def test_clustering_merge_requires_similarity_gate(pg):
    """无相似度下限的并入回归（审计 P0）：

    事件 E1 已存在（成员 M1）；新文章 N 与 E1 同标题（关键词 Jaccard=1.0）
    但向量夹角大（cos=0.80 < 阈值 0.85）。
    旧实现只看关键词（无 sim 下限）→ 误并；新实现必须过三段式门槛 → 独立建事件。
    """
    from app.services.event_service import run_event_clustering

    db = pg()
    try:
        title = "系统更新导致蓝屏问题频发"
        v1 = _vec([1.0, 0.5])                                # 支撑维度 0/1
        perp1 = _vec([0.0, 0.0, 1.0, 0.4])                   # 正交补空间（维度 2/3）
        v2 = _offset(v1, perp1, 0.80)                        # cos(v1, v2) = 0.80 < 阈值
        m1 = _mk_article(db, title=title, source="IT之家", vec=v1, published=NOW - timedelta(hours=20))
        db.add(HotEvent(title=title, summary="", impacts=[], first_time=m1.publish_time,
                        last_time=m1.publish_time, status="发酵中", centroid=v1, timeline=[]))
        db.flush()
        e1 = db.query(HotEvent).one()
        db.add(EventArticle(event_id=e1.id, article_id=m1.id))
        db.commit()

        _mk_article(db, title=title, source="快科技", vec=v2, published=NOW - timedelta(hours=2))
        db.commit()

        stats = run_event_clustering(db)
        events = db.query(HotEvent).order_by(HotEvent.id).all()
        assert len(events) == 2, f"低相似度不得并入已有事件: {stats}"
        m1_linked = [lnk.article_id for lnk in db.query(EventArticle).filter_by(event_id=events[0].id)]
        assert m1_linked == [m1.id]
        n_linked = [lnk.article_id for lnk in db.query(EventArticle).filter_by(event_id=events[1].id)]
        assert len(n_linked) == 1 and n_linked[0] != m1.id
    finally:
        db.close()


def test_clustering_rescue_branch_requires_similarity_floor(pg):
    """成员重叠救援分支加下限（审计 P0 放大器）：

    事件 E1 被历史污染（质心漂移到与成员 M1 无关的方向）。M1 重聚类时
    与 E1 的质心相似度过低 → 救援分支不得零门槛认领（旧实现按重叠数直接并入）。
    """
    from app.services.event_service import run_event_clustering

    db = pg()
    try:
        drifted = _vec([0.0, 0.0, 1.0])  # 漂移质心：与成员向量正交
        m1 = _mk_article(
            db, title="六人利用AI越狱生成淫秽物品获刑", source="IT之家",
            vec=_vec([1.0, 0.2]), published=NOW - timedelta(hours=30),
        )
        db.add(HotEvent(title="污染大杂烩事件", summary="", impacts=[],
                        first_time=m1.publish_time, last_time=m1.publish_time,
                        status="发酵中", centroid=drifted, timeline=[]))
        db.flush()
        e1 = db.query(HotEvent).one()
        db.add(EventArticle(event_id=e1.id, article_id=m1.id))
        db.commit()

        stats = run_event_clustering(db)
        # M1 与污染质心正交（sim=0）：step1 拒绝；救援分支 sim < 阈值也必须拒绝
        # → M1 落入新事件；随后 ① 共享成员去重会把新事件确定性并回 E1（既有行为），
        # 但成员归属不应扩大
        links = db.query(EventArticle).all()
        assert stats["events_updated"] + stats["events_created"] >= 1
        # 无论走哪条路径，事件成员集合不增（没有新文章被错误吸附）
        assert len(links) == 1, f"污染事件不得吸附新成员: {links}"
    finally:
        db.close()


def test_clustering_groups_cross_source_same_event(pg):
    """正向用例：跨来源、高相似、关键词重合的真实同事件文章聚合为一个事件。"""
    from app.services.event_service import run_event_clustering

    db = pg()
    try:
        v = _vec([1.0, 0.3, 0.2])
        _mk_article(db, title="豆包输入法Windows版正式上线", source="IT之家",
                    vec=v, published=NOW - timedelta(hours=5))
        _mk_article(db, title="豆包输入法推出Windows版覆盖全平台", source="快科技",
                    vec=_offset(v, _vec([0.0, 0.0, 0.0, 0.0, 1.0, 0.3]), 0.95),
                    published=NOW - timedelta(hours=3))
        _mk_article(db, title="深海火山口发现新型嗜热细菌群落", source="IT之家",
                    vec=_vec([0.0, 0.0, 0.0, 1.0]), published=NOW - timedelta(hours=4))
        db.commit()

        run_event_clustering(db)
        events = db.query(HotEvent).all()
        assert len(events) == 2
        sizes = sorted(
            db.query(EventArticle).filter_by(event_id=e.id).count() for e in events
        )
        assert sizes == [1, 2]
    finally:
        db.close()


def test_clustering_skips_overaged_events(pg):
    """吸纳年龄上限（2026-10-14 审计新增，防大杂烩滚雪球）：候选资格除 last_time
    新鲜外还要求 first_time 在 EVENT_MAX_AGE_DAYS 内——常青杂烩事件（每天有新
    成员、last_time 恒新鲜）不得再吞新文章（主用例），也不得经事件级合并卷入
    新事件；同龄事件照常并入（对照组）。"""
    from app.services.event_service import EVENT_MAX_AGE_DAYS, run_event_clustering

    db = pg()
    try:
        # 超龄杂烩：first_time 在上限外 7 天，last_time 1 小时前（昨天刚吞过新成员）
        aged_first = NOW - timedelta(days=EVENT_MAX_AGE_DAYS + 7)
        v_win = _vec([1.0, 0.2])
        m_aged = _mk_article(db, title="Win11 五周年旧文", source="IT之家", vec=v_win,
                             published=aged_first)
        db.add(HotEvent(title="微软 Win11 五周年（超龄事件）", summary="", impacts=[],
                        first_time=aged_first, last_time=NOW - timedelta(hours=1),
                        status="发酵中", centroid=v_win, timeline=[]))
        db.flush()
        e_aged = db.query(HotEvent).filter_by(title="微软 Win11 五周年（超龄事件）").one()
        db.add(EventArticle(event_id=e_aged.id, article_id=m_aged.id))
        db.commit()

        # 新文章：与超龄事件成员近同题同向量（旧口径必过三段式门槛被吸入）
        _mk_article(db, title="Win11 五周年：全球超 10 亿用户背后", source="快科技",
                    vec=_offset(v_win, _vec([0.0, 0.0, 1.0, 0.3]), 0.95),
                    published=NOW - timedelta(hours=2))
        # 对照组：同龄事件 + 高相似跨来源新文章，应照常并入
        v_fresh = _vec([0.0, 1.0, 0.3])
        m_fresh = _mk_article(db, title="某新品正式发布", source="IT之家", vec=v_fresh,
                              published=NOW - timedelta(hours=6))
        db.add(HotEvent(title="某新品正式发布", summary="", impacts=[],
                        first_time=m_fresh.publish_time, last_time=m_fresh.publish_time,
                        status="发酵中", centroid=v_fresh, timeline=[]))
        db.flush()
        e_fresh = db.query(HotEvent).filter_by(title="某新品正式发布").one()
        db.add(EventArticle(event_id=e_fresh.id, article_id=m_fresh.id))
        db.commit()
        _mk_article(db, title="某新品正式发布售罄", source="快科技",
                    vec=_offset(v_fresh, _vec([1.0, 0.0, 0.0, 0.0, 0.5]), 0.95),
                    published=NOW - timedelta(hours=1))
        db.commit()

        stats = run_event_clustering(db)
        aged_links = [lnk.article_id for lnk in db.query(EventArticle).filter_by(event_id=e_aged.id)]
        assert aged_links == [m_aged.id], f"超龄事件不得吞新成员: {stats} {aged_links}"
        fresh_links = db.query(EventArticle).filter_by(event_id=e_fresh.id).count()
        assert fresh_links == 2, f"同龄事件对照组应照常并入: {stats}"
    finally:
        db.close()


def test_clustering_llm_review_blocks_unrelated_merge(pg, monkeypatch):
    """LLM「同一事件」审核（2026-10-14 第三批）：大事件（成员 ≥ 门槛）并入前交
    判定，判「另一件事」则拒绝并入、新文章另立事件；小事件不触发审核照常并入
    （对照组，且判定函数只被调用一次）。LLM 配置用回环地址让 URL 守卫就地拒绝，
    全程无外部调用。"""
    from app.services import event_service
    from app.services.event_service import LLM_REVIEW_EVENT_MEMBERS, run_event_clustering

    db = pg()
    try:
        # 大事件：5 篇跨来源成员（恰达审核门槛）
        v_big = _vec([1.0, 0.2])
        big = HotEvent(title="大事件主题", summary="", impacts=[],
                       first_time=NOW - timedelta(hours=30), last_time=NOW - timedelta(hours=2),
                       status="发酵中", centroid=v_big, timeline=[])
        db.add(big)
        db.flush()
        for i in range(LLM_REVIEW_EVENT_MEMBERS):
            m = _mk_article(db, title=f"大事件成员报道第{i}篇",
                            source="IT之家" if i % 2 == 0 else "快科技",
                            vec=v_big, published=NOW - timedelta(hours=20 - i * 3))
            db.add(EventArticle(event_id=big.id, article_id=m.id))
        # 小事件：1 篇成员（低于门槛，不触发审核）
        v_small = _vec([0.0, 1.0, 0.3])
        m_s = _mk_article(db, title="小事件唯一报道", source="IT之家", vec=v_small,
                          published=NOW - timedelta(hours=10))
        small = HotEvent(title="小事件主题", summary="", impacts=[],
                         first_time=m_s.publish_time, last_time=m_s.publish_time,
                         status="发酵中", centroid=v_small, timeline=[])
        db.add(small)
        db.flush()
        db.add(EventArticle(event_id=small.id, article_id=m_s.id))
        db.commit()

        calls: list[str] = []

        def fake_review(event_title, _summary, _members, _news, _llm, event_id=None):
            calls.append(str(event_id))
            return False  # 一律判「另一件事」

        monkeypatch.setattr(event_service, "_llm_same_event", fake_review)
        # 回环 base_url：钩子视为已配置，而摘要链路的真实调用被 URL 守卫就地拒绝
        monkeypatch.setattr(
            event_service.config_service, "effective_llm",
            lambda _db: ("http://127.0.0.1:9/v1", "k", "m", "openai"),
        )

        _mk_article(db, title="大事件主题下的另一件事", source="快科技",
                    vec=_offset(v_big, _vec([0.0, 0.0, 1.0, 0.3]), 0.97),
                    published=NOW - timedelta(hours=1))
        _mk_article(db, title="小事件主题后续报道", source="快科技",
                    vec=_offset(v_small, _vec([1.0, 0.0, 0.0, 0.0, 0.5]), 0.95),
                    published=NOW - timedelta(hours=1))
        db.commit()

        run_event_clustering(db)
        assert db.query(EventArticle).filter_by(event_id=big.id).count() == LLM_REVIEW_EVENT_MEMBERS, \
            "LLM 判「另一件事」后大事件成员数不得增长"
        assert db.query(EventArticle).filter_by(event_id=small.id).count() == 2, \
            "小事件低于审核门槛应照常并入"
        # 恰一次判定（step 4 采纳收口点，目标=大事件）；小事件从未触发
        assert len(calls) == 1 and set(calls) == {str(big.id)}, f"判定调用应恰为大事件一次: {calls}"
    finally:
        db.close()


def test_merge_recent_events_llm_refusal_terminates_without_merge(pg, monkeypatch):
    """二阶段合并的 LLM 审核：两个达标大事件可合并时，判「另事件」则跳过该对
    且循环正常终止（refused_pairs 防重扫死循环）；直接调 _merge_recent_events，
    两事件同标题保证关键词门槛必过，排除 jieba 分词不确定性。"""
    from app.services import event_service
    from app.services.event_service import LLM_REVIEW_EVENT_MEMBERS, _merge_recent_events

    db = pg()
    try:
        v = _vec([1.0, 0.2])
        v2 = _offset(v, _vec([0.0, 0.0, 1.0, 0.3]), 0.95)
        events = []
        for k, vec in (("甲", v), ("乙", v2)):
            ev = HotEvent(title="同标题审核事件", summary="", impacts=[],
                          first_time=NOW - timedelta(days=2), last_time=NOW - timedelta(hours=2),
                          status="发酵中", centroid=vec, timeline=[])
            db.add(ev)
            db.flush()
            for i in range(LLM_REVIEW_EVENT_MEMBERS):
                m = _mk_article(db, title=f"同标题审核事件成员{k}{i}",
                                source="IT之家" if i % 2 == 0 else "快科技",
                                vec=vec, published=NOW - timedelta(days=8) + timedelta(hours=i))
                db.add(EventArticle(event_id=ev.id, article_id=m.id))
            events.append(ev)
        # 一篇窗口内新文章撑起聚类轮（否则 run 级早退；本测试直调合并函数无此约束，
        # 但保持数据形态贴近真实）
        db.commit()

        calls: list[str] = []

        def fake_review(event_title, _summary, _members, _news, _llm, event_id=None):
            calls.append(str(event_id))
            return False

        monkeypatch.setattr(event_service, "_llm_same_event", fake_review)
        merged = _merge_recent_events(
            db, since=NOW - timedelta(hours=4), sim_threshold=0.85,
            keyword_threshold=0.2, window_hours=48,
            llm=("http://127.0.0.1:9/v1", "k", "m", "openai"),
        )
        assert merged == 0, "被判「另一件事」的事件对不得合并"
        assert db.query(EventArticle).filter_by(event_id=events[0].id).count() == LLM_REVIEW_EVENT_MEMBERS
        assert db.query(EventArticle).filter_by(event_id=events[1].id).count() == LLM_REVIEW_EVENT_MEMBERS
        assert len(calls) == 1, f"被拒对只审一次（refused_pairs 防重扫）: {calls}"
    finally:
        db.close()


# ———— 标题重选举 / 事件合并 / 热度重算（2026-09-10 审计补齐）————

def test_event_title_reelected_as_members_grow(pg):
    """标题随成员成长重选举：旧实现只在创建时定标题，事件并入更核心表述的
    新报道后仍顶着最初单篇的标题。

    两成员选举的平票口径 = 取更长标题（Jaccard 对称），故断言确定：首建为
    短标题 t1，并入更长的 t2 后事件标题必须翻转为 t2。"""
    from app.ai.event_cluster import elect_title
    from app.services.event_service import run_event_clustering
    from app.utils.text import extract_keywords

    db = pg()
    try:
        t1 = "豆包输入法海外版上架苹果应用商店"
        t2 = "豆包输入法海外版正式登陆安卓苹果双应用商店"
        v = _vec([1.0, 0.3])
        _mk_article(db, title=t1, source="快科技", vec=v,
                    published=NOW - timedelta(hours=5))
        db.commit()
        run_event_clustering(db)
        ev = db.query(HotEvent).one()
        assert ev.title == t1

        _mk_article(db, title=t2, source="IT之家",
                    vec=_offset(v, _vec([0.0, 0.0, 1.0, 0.2]), 0.95),
                    published=NOW - timedelta(hours=3))
        db.commit()
        run_event_clustering(db)
        assert ev.title == t2
        # 与成员集上的独立重选举同口径（不只满足本例的平票特例）
        member_titles = [
            a.title
            for a in db.query(Article).join(
                EventArticle, EventArticle.article_id == Article.id
            ).all()
        ]
        expected = elect_title(
            member_titles, [set(extract_keywords(t, 10)) for t in member_titles]
        )
        assert ev.title == expected
    finally:
        db.close()


def test_merge_recent_events_shared_member_absorbs(pg):
    """共享成员去重（事件合并 ①，历史救援副作用的确定性修复路径）：
    同一文章挂两个事件 → 成员多者吞并少者（平票取先建 id 小者），
    非重叠成员迁移、质心按全量成员重算、时间跨度扩展、败者删除。"""
    from app.ai.embedding import centroid
    from app.services.event_service import _merge_recent_events

    db = pg()
    try:
        v1, v2, v3 = _vec([1.0, 0.2]), _vec([0.9, 0.3]), _vec([0.8, 0.4])
        m1 = _mk_article(db, title="共享成员文章甲", source="IT之家", vec=v1,
                         published=NOW - timedelta(hours=5))
        m2 = _mk_article(db, title="共享成员文章乙", source="快科技", vec=v2,
                         published=NOW - timedelta(hours=4))
        m3 = _mk_article(db, title="共享成员文章丙", source="IT之家", vec=v3,
                         published=NOW - timedelta(hours=3))
        e1 = HotEvent(title="事件甲", summary="", impacts=[], centroid=centroid([v1, v2]),
                      first_time=m1.publish_time, last_time=m2.publish_time,
                      status="发酵中", timeline=[])
        e2 = HotEvent(title="事件乙", summary="", impacts=[], centroid=centroid([v2, v3]),
                      first_time=m2.publish_time, last_time=m3.publish_time,
                      status="发酵中", timeline=[])
        db.add_all([e1, e2])
        db.flush()
        db.add_all([
            EventArticle(event_id=e1.id, article_id=m1.id),
            EventArticle(event_id=e1.id, article_id=m2.id),
            EventArticle(event_id=e2.id, article_id=m2.id),
            EventArticle(event_id=e2.id, article_id=m3.id),
        ])
        db.commit()

        merged = _merge_recent_events(
            db, since=NOW, sim_threshold=0.85, keyword_threshold=0.2, window_hours=48,
        )
        assert merged == 1
        events = db.query(HotEvent).all()
        assert len(events) == 1 and events[0].id == e1.id
        links = db.query(EventArticle).filter_by(event_id=e1.id).all()
        assert {lnk.article_id for lnk in links} == {m1.id, m2.id, m3.id}
        assert events[0].centroid is not None
        # pgvector 往返是 float32（≈7 位有效数字），1e-6 容差足够区分成员集合错误
        for got, want in zip(list(events[0].centroid), centroid([v1, v2, v3])):
            assert abs(float(got) - want) < 1e-6
        assert _tz(events[0].first_time) == _tz(m1.publish_time)
        assert _tz(events[0].last_time) == _tz(m3.publish_time)
    finally:
        db.close()


def test_recompute_event_scores_sums_members_and_flips_status(pg):
    """热度重算 = 窗口内成员文章 HotScore 之和（DB 内聚合，容许 python/DB 时钟
    微差）；无成员事件归 0；长时间无新报道的事件状态随每轮重算翻牌（旧实现会
    永久冻结在「发酵中」）。成员聚合限 EVENT_SCORE_WINDOW_HOURS 窗口（2026-10-14
    对齐 AIHOT 48h 硬窗口）：窗口外成员不再计入，事件随窗口滑出归零沉底。"""
    import math

    from app.algorithms.hot_score import hot_score
    from app.services.event_service import EVENT_SCORE_WINDOW_HOURS, recompute_event_scores

    db = pg()
    try:
        v = _vec([1.0, 0.1])
        m1 = _mk_article(db, title="有互动的热点文章", source="IT之家", vec=v,
                         published=NOW - timedelta(hours=10), views=100)
        m_stale = _mk_article(db, title="窗口外的高互动旧文", source="快科技", vec=v,
                              published=NOW - timedelta(hours=EVENT_SCORE_WINDOW_HOURS + 12),
                              views=5000)
        e1 = HotEvent(title="热点事件", summary="", impacts=[], centroid=v,
                      first_time=m_stale.publish_time, last_time=m1.publish_time,
                      status="发酵中", hot_score=0.0, timeline=[])
        e2 = HotEvent(title="无成员事件", summary="", impacts=[],
                      first_time=NOW - timedelta(hours=48),
                      last_time=NOW - timedelta(hours=48),
                      status="发酵中", hot_score=999.0, timeline=[])
        db.add_all([e1, e2])
        db.flush()
        db.add_all([
            EventArticle(event_id=e1.id, article_id=m1.id),
            EventArticle(event_id=e1.id, article_id=m_stale.id),
        ])
        db.commit()

        recompute_event_scores(db)
        expected = hot_score(100, 0, 0, 0, m1.publish_time)
        # 严格等于窗口内成员之和：窗口外 5000 阅读的旧文若泄漏进聚合，
        # 分数必然高于 expected（ln(5001) 量纲远大于 ln(101)），isclose 即翻车
        assert math.isclose(e1.hot_score, round(expected, 2), rel_tol=0.01)
        assert e1.status == "持续关注"  # last_time 在 6~24h 内
        assert e2.hot_score == 0.0
        assert e2.status == "已平息"
    finally:
        db.close()


# ———— build_report：早报窗口口径（审计 P1；2026-09-17 改「昨夜今晨」窗口） ————

def test_build_report_event_stats_window_scoped(pg):
    """早报事件统计限定窗口内：跨日事件的 articles_count 只数窗口内成员，
    全生命周期数字放 lifecycle_* 字段。

    窗口口径（2026-09-17 早报改版）：build_report(db, day) 的 day 为发布日，
    窗口 = 北京时间 (day-1) 00:00 → day 08:00「昨夜今晨」——overnight
    （00:00–08:00）报道归当期而非顺延一天，win_end 严格排他。"""
    from app.services.report_service import build_report

    db = pg()
    try:
        day = (NOW - timedelta(days=1)).date()  # 发布日
        win_start = datetime(day.year, day.month, day.day, tzinfo=TZ_BJ) - timedelta(days=1)
        win_end = datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ_BJ)
        v = _vec([1.0, 0.1])
        in_win_a = _mk_article(db, title="某事件窗口内报道A", source="IT之家", vec=v,
                               published=win_start + timedelta(hours=10))
        out_win_a = _mk_article(db, title="某事件窗口外报道B", source="快科技", vec=v,
                                published=win_start - timedelta(hours=14))
        # overnight：今晨 06:00 发布，旧自然日窗口会漏掉，新窗口必须归入当期
        overnight_a = _mk_article(db, title="今晨overnight报道C", source="IT之家", vec=v,
                                  published=win_end - timedelta(hours=2))
        # 边界：恰在 win_end 08:00 发布（排他），不得计入；其唯一来源快科技
        # 若被误计入会把 sources_count 抬到 2，构成精确的边界探针
        boundary_a = _mk_article(db, title="八点截稿后报道D", source="快科技", vec=v,
                                 published=win_end)
        db.add(HotEvent(title="跨日事件标题", summary="事件总结", impacts=[],
                        first_time=out_win_a.publish_time, last_time=overnight_a.publish_time,
                        status="发酵中", centroid=v, timeline=[]))
        db.flush()
        ev = db.query(HotEvent).one()
        db.add_all([
            EventArticle(event_id=ev.id, article_id=in_win_a.id),
            EventArticle(event_id=ev.id, article_id=out_win_a.id),
            EventArticle(event_id=ev.id, article_id=overnight_a.id),
            EventArticle(event_id=ev.id, article_id=boundary_a.id),
            AiUsageLog(
                created_at=win_start + timedelta(hours=1),
                task_type="summary", model="test-model", total_tokens=100, cost=0.25,
            ),
            AiUsageLog(
                created_at=win_start - timedelta(seconds=1),
                task_type="summary", model="test-model", total_tokens=200, cost=1.00,
            ),
            # 昨日 24:00（= day 00:00）旧窗口已结束，新「昨夜今晨」窗口仍开启：计入
            AiUsageLog(
                created_at=datetime(day.year, day.month, day.day, tzinfo=TZ_BJ),
                task_type="summary", model="test-model", total_tokens=300, cost=2.00,
            ),
            # 今晨 08:00 整点：win_end 排他，不计入
            AiUsageLog(
                created_at=win_end,
                task_type="summary", model="test-model", total_tokens=400, cost=4.00,
            ),
        ])
        db.commit()

        report = build_report(db, day)
        top = {e["id"]: e for e in report["top_events"]}
        assert top[ev.id]["articles_count"] == 2  # 窗口内 2 篇（A + overnight C）
        assert top[ev.id]["sources_count"] == 1  # 窗口内仅 IT之家（边界 D 的快科技被排除）
        assert top[ev.id]["lifecycle_articles_count"] == 4  # 全周期 4 篇
        assert top[ev.id]["lifecycle_sources_count"] == 2
        # AI 成本窗口随早报窗口：含昨日午夜后、不含今晨 08:00 整点
        assert report["ai_cost"] == {"calls": 2, "tokens": 400, "cost": 2.25}
    finally:
        db.close()


def test_build_report_event_ranking_window_scoped(pg):
    """事件排序分改窗口内独立来源口径（2026-10-14）：全生命周期存量 hot_score
    退出早报排序——常青大事件窗口内仅 1 篇零互动弱文时，不得再凭历史成员
    碾压当日新热点（回归探针：存量 hot_score=9999 若回流排序即翻车）。"""
    from app.services.report_service import build_report

    db = pg()
    try:
        day = (NOW - timedelta(days=1)).date()  # 发布日
        win_start = datetime(day.year, day.month, day.day, tzinfo=TZ_BJ) - timedelta(days=1)
        v = _vec([1.0, 0.1])
        # 常青大事件：21 天前建簇、存量 2 篇旧文、窗口内仅 1 篇零互动弱文
        m_hist1 = _mk_article(db, title="常青事件历史报道一", source="IT之家", vec=v,
                              published=NOW - timedelta(days=21))
        m_hist2 = _mk_article(db, title="常青事件历史报道二", source="快科技", vec=v,
                              published=NOW - timedelta(days=21) + timedelta(hours=2))
        m_weak = _mk_article(db, title="常青事件窗口内弱报道", source="IT之家", vec=v,
                             published=win_start + timedelta(hours=2))
        db.add(HotEvent(title="常青大事件旧标题", summary="", impacts=[],
                        first_time=m_hist1.publish_time, last_time=m_weak.publish_time,
                        status="发酵中", centroid=v, timeline=[], hot_score=9999.0))
        db.flush()
        e_old = db.query(HotEvent).one()
        db.add_all([
            EventArticle(event_id=e_old.id, article_id=m_hist1.id),
            EventArticle(event_id=e_old.id, article_id=m_hist2.id),
            EventArticle(event_id=e_old.id, article_id=m_weak.id),
        ])
        # 当日新事件：窗口内 1 篇带阅读量的报道（更新、互动更高 → 窗口分必胜）
        v_new = _vec([0.0, 1.0, 0.2])
        m_new = _mk_article(db, title="当日新热点报道", source="IT之家", vec=v_new,
                            published=win_start + timedelta(hours=20), views=800)
        db.add(HotEvent(title="当日新热点", summary="", impacts=[],
                        first_time=m_new.publish_time, last_time=m_new.publish_time,
                        status="发酵中", centroid=v_new, timeline=[], hot_score=1.0))
        db.flush()
        e_new = db.query(HotEvent).filter_by(title="当日新热点").one()
        db.add(EventArticle(event_id=e_new.id, article_id=m_new.id))
        db.commit()

        report = build_report(db, day)
        order = [e["id"] for e in report["top_events"]]
        assert order[0] == e_new.id, f"当日新热点应居首（窗口口径）: {order}"
        old = next(e for e in report["top_events"] if e["id"] == e_old.id)
        assert old["articles_count"] == 1 and old["lifecycle_articles_count"] == 3
        assert old["hot_score"] < report["top_events"][0]["hot_score"] <= 100.0
    finally:
        db.close()


def test_build_report_suppresses_previously_featured_events(pg):
    """重复抑制（2026-10-14，AIHOT 日报 MEMORY_DAYS 同款）：最近 7 期上过事件
    栏目的事件今日打 follow_up 标垫底补位，新鲜事件优先占位——即使回访事件
    窗口分更高（回归探针：无抑制时它按分会排第一）；8 天前的记忆过期不抑制。"""
    from app.services.report_service import build_report

    db = pg()
    try:
        day = (NOW - timedelta(days=1)).date()  # 发布日
        win_start = datetime(day.year, day.month, day.day, tzinfo=TZ_BJ) - timedelta(days=1)
        v = _vec([1.0, 0.1])
        # 回访事件：昨日头条，今日窗口分更高（成员更新、互动更高）
        m_rep = _mk_article(db, title="回访事件今日报道", source="IT之家", vec=v,
                            published=win_start + timedelta(hours=20), views=800)
        db.add(HotEvent(title="回访事件", summary="", impacts=[],
                        first_time=win_start - timedelta(days=2),
                        last_time=m_rep.publish_time, status="发酵中", centroid=v, timeline=[]))
        db.flush()
        e_rep = db.query(HotEvent).filter_by(title="回访事件").one()
        db.add(EventArticle(event_id=e_rep.id, article_id=m_rep.id))
        # 新鲜事件：今日窗口分较低
        v_fresh = _vec([0.0, 1.0, 0.2])
        m_fresh = _mk_article(db, title="新鲜事件今日报道", source="快科技", vec=v_fresh,
                              published=win_start + timedelta(hours=4), views=100)
        db.add(HotEvent(title="新鲜事件", summary="", impacts=[],
                        first_time=m_fresh.publish_time, last_time=m_fresh.publish_time,
                        status="发酵中", centroid=v_fresh, timeline=[]))
        db.flush()
        e_fresh = db.query(HotEvent).filter_by(title="新鲜事件").one()
        db.add(EventArticle(event_id=e_fresh.id, article_id=m_fresh.id))
        # 记忆面：昨日早报头条 = 回访事件；8 天前早报 = 新鲜事件（超出回看期）
        db.add(DailyReport(date=day - timedelta(days=1), issue_no=1,
                           content={"top_events": [{"id": e_rep.id, "title": "回访事件"}]}))
        db.add(DailyReport(date=day - timedelta(days=8), issue_no=1,
                           content={"top_events": [{"id": e_fresh.id, "title": "新鲜事件"}]}))
        db.commit()

        report = build_report(db, day)
        order = [e["id"] for e in report["top_events"]]
        assert order[0] == e_fresh.id, f"新鲜事件应优先占位: {order}"
        rep_entry = next(e for e in report["top_events"] if e["id"] == e_rep.id)
        assert rep_entry.get("follow_up") is True
        fresh_entry = next(e for e in report["top_events"] if e["id"] == e_fresh.id)
        assert "follow_up" not in fresh_entry
    finally:
        db.close()


# ———— build_report：热榜 TOP10 与热点速览互斥（2026-09-19 热榜 TOP5 → TOP10） ————

def test_build_report_top_articles_top10_disjoint_from_quick(pg):
    """昨夜今晨热榜扩为 TOP10（2026-09-19）：top_articles 窗口内取满 10 条；
    热点速览取池自 scored[10:] 起步——与热榜同页零重复（防回归为旧 TOP5 口径）。"""
    from app.services.report_service import build_report

    db = pg()
    try:
        day = (NOW - timedelta(days=1)).date()  # 发布日
        win_start = datetime(day.year, day.month, day.day, tzinfo=TZ_BJ) - timedelta(days=1)
        v = _vec([1.0, 0.1])
        # 窗口内 14 篇零互动文章：热度由时间衰减主导，发布越晚分越高（排序确定）
        for i in range(14):
            _mk_article(db, title=f"热榜候选报道第{i}号", source="IT之家", vec=v,
                        published=win_start + timedelta(hours=10 + i))
        db.commit()

        report = build_report(db, day)
        assert len(report["top_articles"]) == 10  # 热榜取满 10 条（旧口径只有 5）
        top_ids = {a["id"] for a in report["top_articles"]}
        quick_ids = {a["id"] for a in report["quick_news"]}
        assert quick_ids  # 池子够深（14 > 10），速览仍有增量条目
        assert not (top_ids & quick_ids)  # 热榜 TOP10 与速览不得同页两遍
    finally:
        db.close()


# ———— generate_daily_report：期号递增与同日幂等（审计：早报真实生成链路无集成测试） ————

def test_generate_daily_report_issue_no_and_idempotency(pg):
    """同日重复生成更新同一行（advisory lock + 更新分支，issue_no 不变）；
    次日生成期号严格 +1（issue_no_for 以 report_issue_epoch 为基准）。"""
    from app.services.report_service import generate_daily_report

    db = pg()
    try:
        day = (NOW - timedelta(days=2)).date()
        row1, _ = generate_daily_report(db, day)
        issue1 = row1.issue_no
        assert row1.date == day

        # 同日重复生成：幂等——同一行、同期号（手动重触发/定时撞车不产生重复期）
        row2, _ = generate_daily_report(db, day)
        assert row2.id == row1.id
        assert row2.issue_no == issue1

        # 次日：新行 + 期号递增
        row3, _ = generate_daily_report(db, day + timedelta(days=1))
        assert row3.id != row1.id
        assert row3.issue_no == issue1 + 1

        # 跳一天生成：期号按日期差递增（不要求连续生成）
        row4, _ = generate_daily_report(db, day + timedelta(days=3))
        assert row4.issue_no == issue1 + 3
    finally:
        db.close()


# ———— generate_daily_report：推送策略（2026-09-10 审计收紧） ————

def test_generate_daily_report_push_policy(pg, monkeypatch):
    """自动推送仅限「发布日=今天（当期新刊）」且内容非空：空报告不推、历史补算不推；
    推送状态经返回值透出（task_run.detail 由此写入，面板可见）。"""
    from app.services import report_service

    queued_dates: list[str] = []
    monkeypatch.setattr(report_service, "push_enabled", lambda db: True)

    def fake_enqueue(db, day, content):
        queued_dates.append(content["date"])
        return 1

    monkeypatch.setattr(report_service, "enqueue_report_deliveries", fake_enqueue)

    db = pg()
    try:
        today = (NOW + timedelta(hours=8)).date()  # 北京时间今天（发布日口径）

        # 空报告：不推送（采集停摆时不向群里灌空刊，任务停摆另有告警通道）
        _, status = report_service.generate_daily_report(db, today)
        assert status == "内容为空，未推送"
        assert queued_dates == []

        # 当期新刊（发布日=今天，窗口=北京昨夜今晨）且有内容：推送并透出状态；
        # 综述走模板兜底（测试环境无 LLM）
        v = _vec([1.0, 0.0])
        win_start = datetime(today.year, today.month, today.day, tzinfo=TZ_BJ) - timedelta(days=1)
        _mk_article(db, title="昨日有内容的报道", source="IT之家", vec=v,
                    published=win_start + timedelta(hours=9))
        db.commit()
        row_today, status = report_service.generate_daily_report(db, today)
        assert status == "已加入推送队列 1 个通道"
        assert row_today.content["overview_source"] == "template"
        assert queued_dates == [today.isoformat()]

        # 历史日期补算：即使有内容也不推（不再向群通道灌旧闻）
        old_day = today - timedelta(days=5)
        old_start = datetime(old_day.year, old_day.month, old_day.day, tzinfo=TZ_BJ) - timedelta(days=1)
        _mk_article(db, title="五天前的旧闻", source="快科技", vec=v,
                    published=old_start + timedelta(hours=9))
        db.commit()
        _, status = report_service.generate_daily_report(db, old_day)
        assert status == "历史日期补算，未推送"
        assert queued_dates == [today.isoformat()]
    finally:
        db.close()


# ———— catchup_missing_reports：错过 08:00 调度的补偿口径（2026-09-17 审计） ————

def test_catchup_missing_reports(pg):
    """回看 3 天无行才补（含占位空刊也算有行）、epoch 前跳过；
    今天仅过了 08:00 截稿才纳入——早于 08:00 补生成会以不完整窗口顶出当期。"""
    from app.services.report_service import catchup_missing_reports

    db = pg()
    try:
        today = (NOW + timedelta(hours=8)).date()  # 北京时间今天
        # 第 1 期基准日 2026-04-15 之后的日期才会进回看窗
        db.add(DailyReport(date=today - timedelta(days=1), issue_no=999,
                           content={"top_events": [], "quick_news": []}))  # 占位空刊也算有行
        db.commit()

        def _at(hour: int):
            return catchup_missing_reports(
                db, now=datetime(today.year, today.month, today.day, hour, 45, tzinfo=TZ_BJ)
            )

        # 07:45（08:00 前）：只补历史缺口 today-3 / today-2（today-1 已有行）
        assert _at(7) == [today - timedelta(days=3), today - timedelta(days=2)]
        # 08:45（截稿后）：今天仍无行，一并补
        assert _at(9) == [today - timedelta(days=3), today - timedelta(days=2), today]
    finally:
        db.close()


# ———— /report/daily latest：全空占位报告不顶掉有效报告 ————

def test_report_daily_latest_skips_empty_placeholder(pg):
    """latest 跳过全空报告（凌晨窗口刚开启时的误生成占位），回退最近一个有实质内容的；
    指定 date 仍可原样访问（审计口径：早报页不应白天展示一整天空内容）。"""
    from fastapi import HTTPException

    from app.api.v2.routes import report_daily

    db = pg()
    try:
        good_day = (NOW - timedelta(days=1)).date()
        empty_day = good_day + timedelta(days=1)
        db.add(DailyReport(date=good_day, issue_no=1,
                           content={"top_events": [{"id": 9}], "quick_news": []}))
        db.add(DailyReport(date=empty_day, issue_no=2,
                           content={"top_events": [], "quick_news": []}))
        db.commit()

        latest = report_daily(date=None, db=db)
        assert latest["date"] == good_day.isoformat()

        by_date = report_daily(date=empty_day.isoformat(), db=db)
        assert by_date["date"] == empty_day.isoformat()

        with pytest.raises(HTTPException) as ei:
            report_daily(date="2099-01-01", db=db)
        assert ei.value.status_code == 404
    finally:
        db.close()


# ———— /report/dates：占位空刊不进期号列表（2026-09-10 审计） ————

def test_report_dates_hides_empty_placeholder(pg):
    """前端默认选中期号列表首项：空刊在列 = 早报页默认展示空内容。
    期号列表只收录有实质内容的期；次日定时任务覆盖空刊日期行后自然复现。"""
    from app.api.v2.routes import report_dates

    db = pg()
    try:
        good_day = (NOW - timedelta(days=1)).date()
        empty_day = good_day + timedelta(days=1)
        db.add(DailyReport(date=good_day, issue_no=1,
                           content={"top_events": [{"id": 9}], "quick_news": [{"id": 3}]}))
        db.add(DailyReport(date=empty_day, issue_no=2, content={"top_events": [], "quick_news": []}))
        db.commit()

        items = report_dates(limit=60, db=db)["items"]
        assert [i["date"] for i in items] == [good_day.isoformat()]
    finally:
        db.close()


# ———— /admin/report/push-test：自检推送（2026-09-11 二轮审计） ————

def test_report_push_test_endpoint(pg, monkeypatch):
    """未启用 → 400；启用后推送最近一期有内容的快照（占位空刊不作样张），
    通道状态串经返回值透出（面板「测试推送」按钮消费）。"""
    from fastapi import HTTPException

    from app.api.v2.routes import test_report_push
    from app.services import config_service, report_push

    db = pg()
    try:
        # 未启用：400 带配置指引
        with pytest.raises(HTTPException) as ei:
            test_report_push(request=None, db=db)
        assert ei.value.status_code == 400
        assert "总开关" in ei.value.detail

        # 启用 + 打桩 push_report（不真发 HTTP）
        config_service.set_overrides(db, {
            "report_push_enabled": "true",
            "report_push_wecom_webhook_url": "https://1.2.3.4/hook",
        })
        seen: dict = {}

        def fake_push(d, content):
            seen["date"] = content["date"]
            return "wecom:ok"

        monkeypatch.setattr(report_push, "push_report", fake_push)

        # 最新一期是空占位 → 跳过，取有内容的上一期作样张
        good_day = (NOW - timedelta(days=1)).date()
        empty_day = good_day + timedelta(days=1)
        db.add(DailyReport(date=good_day, issue_no=1,
                           content={"date": good_day.isoformat(),
                                    "top_events": [{"id": 1}], "quick_news": []}))
        db.add(DailyReport(date=empty_day, issue_no=2,
                           content={"date": empty_day.isoformat(),
                                    "top_events": [], "quick_news": []}))
        db.commit()

        out = test_report_push(request=None, db=db)
        assert out == {"results": "wecom:ok"}
        assert seen["date"] == good_day.isoformat()
    finally:
        db.close()


# ———— /hot/board：窗口参数与稳定排序（审计 P1/P2） ————

def test_hot_board_windows_and_stable_order(pg):
    from app.api.v2.routes import hot_board

    db = pg()
    try:
        v = _vec([1.0, 0.0])
        base = {"channel": "智能时代", "vec": v}
        hot = _mk_article(db, title="十小时前热文", source="IT之家", views=1000,
                          published=NOW - timedelta(hours=10), **base)
        mid = _mk_article(db, title="两天前文章", source="IT之家", views=1000,
                          published=NOW - timedelta(hours=50), **base)
        old = _mk_article(db, title="九天前文章", source="IT之家", views=1000,
                          published=NOW - timedelta(hours=200), **base)
        # 同分同发布时间的并列对：验证 id 降级稳定排序
        tie_a = _mk_article(db, title="并列文章甲", source="IT之家", views=999,
                            published=NOW - timedelta(hours=12), **base)
        tie_b = _mk_article(db, title="并列文章乙", source="IT之家", views=999,
                            published=NOW - timedelta(hours=12), **base)
        assert tie_a.id < tie_b.id
        db.commit()

        r24 = hot_board(channel=None, window="24h", limit=40, db=db)
        ids24 = [i["id"] for i in r24["items"]]
        assert hot.id in ids24 and mid.id not in ids24 and old.id not in ids24
        assert r24["total"] >= 3  # 窗口内总数（含并列对）

        r3d = hot_board(channel=None, window="3d", limit=40, db=db)
        ids3d = [i["id"] for i in r3d["items"]]
        assert mid.id in ids3d and old.id not in ids3d

        rall = hot_board(channel=None, window="all", limit=40, db=db)
        ids_all = [i["id"] for i in rall["items"]]
        assert old.id in ids_all

        # 并列同分：id 大者排前（稳定二级排序，不随刷新抖动）
        assert ids24.index(tie_b.id) < ids24.index(tie_a.id)
    finally:
        db.close()


def test_hot_board_score_consistent_across_windows_and_latest(pg, monkeypatch):
    """统一归一化基准（2026-09-13 审计 P1-4）：同一篇文章的 hot_score 在热榜
    各窗口与最新资讯四处恒等，且所有展示分 ≤100（防 max/列表快照差竞态）。
    """
    from app.api.v2 import routes as routes_mod

    # 隔离 Redis 响应缓存：单测内直连 DB，防跨用例读到别的用例写入的缓存
    monkeypatch.setattr(routes_mod, "cache_get_json", lambda key: None)
    monkeypatch.setattr(routes_mod, "cache_set_json", lambda key, val: None)

    db = pg()
    try:
        base = {"channel": "智能时代", "vec": _vec([1.0, 0.0])}
        target = _mk_article(db, title="跨页一致文章", source="IT之家", views=2000,
                             published=NOW - timedelta(hours=5), **base)
        _mk_article(db, title="更大的爆文", source="IT之家", views=90000,
                    published=NOW - timedelta(hours=6), **base)
        db.commit()

        scores = {}
        for win in ("24h", "7d", "all"):
            r = routes_mod.hot_board(channel=None, window=win, limit=40, db=db)
            item = next(i for i in r["items"] if i["id"] == target.id)
            scores[win] = item["hot_score"]
        r_latest = routes_mod.articles_latest(channel=None, q=None, limit=50, offset=0, db=db)
        item = next(i for i in r_latest["items"] if i["id"] == target.id)
        scores["latest"] = item["hot_score"]

        assert len(set(scores.values())) == 1, f"跨页分数不一致: {scores}"
        assert all(0 <= s <= 100 for s in scores.values())
    finally:
        db.close()
