"""LLM 降级自愈（repair_llm_fallback_summaries）回归测试。

场景：LLM 重试仍失败（api_fail）的文章永久停留抽取式摘要，无人补上。
本文件锁定自愈的四条边界：
1. LLM 已配置 + 近期 + 抽取式摘要 → 补派；
2. LLM 未配置 → 不动作（重派只会白跑一轮抽取式）；
3. 摘要已是 LLM 模型 → 不命中；
4. 超出时间窗口的老文章 → 不回填（成本有界）。
"""
import types
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.models.article import Article
from app.models.summary import ArticleSummary
from app.services import config_service
from app.services.summary_service import repair_llm_fallback_summaries


def _mk_article(db, *, title="标题", hours_old=1.0, summary_model="extractive-textrank-v2"):
    s = db.factory()
    a = Article(
        title=title,
        url=f"https://x.test/{title}",
        channel="智能时代",
        source="IT之家",
        news_id=title,
        content="正文",
        publish_time=datetime.now(timezone.utc) - timedelta(hours=hours_old),
        views=0, comments=0, shares=0, likes=0,
        ai_status="summarized",
    )
    s.add(a)
    s.flush()
    # 自愈窗口按 created_at 判定：显式回写以模拟不同年龄的文章
    a.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    s.add(ArticleSummary(
        article_id=a.id,
        summary_short="抽取式摘要",
        summary_long="抽取式摘要",
        key_points=[],
        tags=[],
        model=summary_model,
    ))
    s.commit()
    aid = a.id
    s.close()
    return aid


def _set_llm(db, configured: bool):
    s = db.factory()
    for k in ("llm_base_url", "llm_api_key", "llm_model"):
        row = s.get(config_service.SystemSetting, k)
        val = ("https://x.test", "k", "m")[("llm_base_url", "llm_api_key", "llm_model").index(k)]
        if configured:
            if row is None:
                s.add(config_service.SystemSetting(key=k, value=val))
            else:
                row.value = val
        elif row is not None:
            s.delete(row)
    s.commit()
    s.close()
    # Settings 缓存里的 .env 值可能漂移：测试环境 .env 无 LLM 键，DB 覆盖即生效；
    # 未配置分支把 DB 覆盖删干净即可（.env 默认空）


def _patch_delay(monkeypatch, dispatched: list):
    from app.tasks import tasks as tasks_mod

    monkeypatch.setattr(tasks_mod.summary_task, "delay", lambda aid: dispatched.append(aid))


def test_repair_dispatches_extractive_summary_when_llm_configured(db, monkeypatch):
    aid = _mk_article(db)
    _set_llm(db, configured=True)
    dispatched: list = []
    _patch_delay(monkeypatch, dispatched)

    s = db.factory()
    out = repair_llm_fallback_summaries(s)
    s.close()
    assert aid in out and dispatched == [aid]


def test_repair_noop_when_llm_unconfigured(db, monkeypatch):
    aid = _mk_article(db)
    _set_llm(db, configured=False)
    dispatched: list = []
    _patch_delay(monkeypatch, dispatched)

    s = db.factory()
    assert repair_llm_fallback_summaries(s) == []
    assert dispatched == []


def test_repair_skips_llm_summaries_and_old_articles(db, monkeypatch):
    _mk_article(db, title="新的", hours_old=1.0, summary_model="deepseek-flash")  # 已是 LLM
    _mk_article(db, title="旧的", hours_old=30.0)  # 超出 24h 窗口
    _set_llm(db, configured=True)
    dispatched: list = []
    _patch_delay(monkeypatch, dispatched)

    s = db.factory()
    assert repair_llm_fallback_summaries(s) == []
    assert dispatched == []


def test_repair_respects_limit(db, monkeypatch):
    for i in range(3):
        _mk_article(db, title=f"篇{i}", hours_old=1.0)
    _set_llm(db, configured=True)
    dispatched: list = []
    _patch_delay(monkeypatch, dispatched)

    s = db.factory()
    out = repair_llm_fallback_summaries(s, limit=2)
    s.close()
    assert len(out) == 2 and len(dispatched) == 2


# ———— 事件总结降级自愈（repair_fallback_event_summaries） ————

def _mk_event(db, *, hours_old=1.0, summary="围绕「某事件」共有 5 篇来自不同报道的关联文章：。"):
    from app.models.event import HotEvent

    s = db.factory()
    ev = HotEvent(
        title="事件", summary=summary,
        last_time=datetime.now(timezone.utc) - timedelta(hours=hours_old),
    )
    s.add(ev)
    s.commit()
    eid = ev.id
    s.close()
    return eid


def test_event_repair_rebuilds_fallback_summary(db, monkeypatch):
    from app.services import event_service

    eid = _mk_event(db)
    _set_llm(db, configured=True)
    rebuilt: list = []
    monkeypatch.setattr(event_service, "_event_members", lambda s, eid: ["成员A", "成员B"])
    monkeypatch.setattr(
        event_service, "_rebuild_event_content",
        lambda s, ev, members: rebuilt.append(ev.id),
    )

    s = db.factory()
    assert event_service.repair_fallback_event_summaries(s) == 1
    assert rebuilt == [eid]


def test_event_repair_noop_when_unconfigured(db, monkeypatch):
    from app.services import event_service

    _mk_event(db)
    _set_llm(db, configured=False)
    monkeypatch.setattr(event_service, "_rebuild_event_content",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应重算")))

    s = db.factory()
    assert event_service.repair_fallback_event_summaries(s) == 0


def test_event_repair_skips_llm_summary_and_old_events(db, monkeypatch):
    from app.services import event_service

    _mk_event(db, hours_old=1.0, summary="LLM 撰写的事件总结，不以拼装引子开头。")
    _mk_event(db, hours_old=30.0)  # 超窗：事实拼装也不回填
    _set_llm(db, configured=True)
    monkeypatch.setattr(event_service, "_rebuild_event_content",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不应重算")))

    s = db.factory()
    assert event_service.repair_fallback_event_summaries(s) == 0
