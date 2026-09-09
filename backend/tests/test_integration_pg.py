"""数据库集成测试（2026-09-09 审计补齐：run_event_clustering / 日报窗口 / 热榜窗口）。

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

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.article import Article  # noqa: F401  注册 metadata
from app.models.event import EventArticle, HotEvent
from app.models.report import DailyReport  # noqa: F401
from app.models.setting import SystemSetting  # noqa: F401
from app.models.summary import ArticleSummary  # noqa: F401
from app.models.task_run import TaskRun  # noqa: F401
from app.models.usage import AiUsageLog, ModelPrice  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="需要 TEST_DATABASE_URL 指向含 pgvector 的 postgres 测试库",
)

TABLES = (
    "article", "article_summary", "event", "event_article", "daily_report",
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


@pytest.fixture()
def pg():
    """独立测试库会话工厂：每用例前清空相关表，结束再清一次。"""
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    from app.core.db import Base

    Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in TABLES])
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _clean():
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


# ———— build_report：日报窗口口径（审计 P1） ————

def test_build_report_event_stats_window_scoped(pg):
    """日报事件统计限定窗口内：跨日事件的 articles_count 只数当天成员，
    全生命周期数字放 lifecycle_* 字段。"""
    from app.services.report_service import build_report

    db = pg()
    try:
        day = (NOW - timedelta(days=1)).date()  # UTC 日界（与后端口径一致）
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        v = _vec([1.0, 0.1])
        today_a = _mk_article(db, title="某事件当天报道A", source="IT之家", vec=v,
                              published=day_start + timedelta(hours=10))
        yesterday_a = _mk_article(db, title="某事件前一日报道B", source="快科技", vec=v,
                                  published=day_start - timedelta(hours=14))
        db.add(HotEvent(title="跨日事件标题", summary="事件总结", impacts=[],
                        first_time=yesterday_a.publish_time, last_time=today_a.publish_time,
                        status="发酵中", centroid=v, timeline=[]))
        db.flush()
        ev = db.query(HotEvent).one()
        db.add_all([
            EventArticle(event_id=ev.id, article_id=today_a.id),
            EventArticle(event_id=ev.id, article_id=yesterday_a.id),
        ])
        db.commit()

        report = build_report(db, day)
        top = {e["id"]: e for e in report["top_events"]}
        assert top[ev.id]["articles_count"] == 1  # 仅当天 1 篇
        assert top[ev.id]["sources_count"] == 1  # 当天仅 IT之家
        assert top[ev.id]["lifecycle_articles_count"] == 2  # 全周期 2 篇
        assert top[ev.id]["lifecycle_sources_count"] == 2
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
