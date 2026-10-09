"""仅在独立 PostgreSQL 测试库中运行；随机 schema 结束时由共享 pg_env 清理。"""
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test_article_draft_pg import TEST_DATABASE_URL, pg_env  # noqa: F401 - 复用现有测试库保护与随机 schema fixture

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires isolated TEST_DATABASE_URL")


def test_upgrade_existing_drafts_preserves_legacy_rows(pg_env):
    engine, factory, _ = pg_env
    from app.models.article_draft import ArticleDraft
    from app.services.article_draft_schema import upgrade_article_draft_columns

    ArticleDraft.__table__.drop(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE article_draft (id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '')"))
        conn.execute(text("INSERT INTO article_draft (id, title) VALUES (41, 'old draft')"))
        upgrade_article_draft_columns(conn)
        upgrade_article_draft_columns(conn)  # 二次启动必须幂等
        legacy = conn.execute(text("SELECT title, channel, revision, source_refs, seo_keywords, software_id, is_pinned FROM article_draft WHERE id=41")).one()
        assert legacy[0:3] == ("old draft", "wechat", 1)
        assert legacy[3] == [] and legacy[4] == "" and legacy[5] == 0 and legacy[6] is False
        indexes = conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() AND tablename='article_draft'")).scalars().all()
        assert "ix_article_draft_channel" in indexes


def test_site_generator_respects_shared_postgres_draft_lock(pg_env, monkeypatch):
    _, factory, _ = pg_env
    from app.models.article_draft import ArticleDraft
    import app.services.site_article_gen_service as service

    setup = factory()
    draft = ArticleDraft(channel="site", source_type="event", source_id=1, source_title="site", tier="brief", status="generating")
    setup.add(draft); setup.commit(); draft_id = draft.id; setup.close()
    holder = factory()
    holder.execute(text("SELECT pg_advisory_xact_lock(hashtext('article_draft:' || :i))"), {"i": draft_id})
    results: list[tuple[bool, str]] = []
    calls: list[int] = []
    monkeypatch.setattr(service.llm_client, "chat", lambda *_args, **_kwargs: calls.append(1))
    def run():
        session = factory()
        try:
            results.append(service.generate_site_article_for_draft(session, draft_id, generation=1))
        finally:
            session.close()
    thread = threading.Thread(target=run)
    try:
        thread.start(); thread.join(8)
        assert not thread.is_alive()
    finally:
        holder.rollback(); holder.close()
    assert results == [(False, "another generation in flight")]
    assert calls == []


def test_site_and_wechat_budget_use_same_pg_lock(pg_env, monkeypatch):
    _, factory, _ = pg_env
    from app.models.audit_log import AuditLog
    import app.api.v2.drafts as wechat_api
    import app.api.v2.site_drafts as site_api

    user = SimpleNamespace(id=999010, email="test@invalid.local", tenant_id=None)
    monkeypatch.setattr(wechat_api.config_service, "get_effective", lambda _db, key: 1 if key == "article_gen_hourly_limit" else "true")
    ready = threading.Event()
    outcome = []
    def site_first():
        db = factory()
        try:
            site_api._check_budget(db, user)
            ready.set()
            from datetime import datetime, timezone
            db.add(AuditLog(action="site_draft.generated", created_at=datetime.now(timezone.utc), user_id=user.id, detail={}))
            db.commit()
            outcome.append("site-reserved")
        finally:
            db.close()
    def wechat_second():
        ready.wait(5)
        db = factory()
        try:
            try:
                wechat_api._check_hourly_budget(db, user)
                outcome.append("overspent")
            except Exception as exc:
                outcome.append(getattr(exc, "status_code", None))
            db.rollback()
        finally:
            db.close()
    a, b = threading.Thread(target=site_first), threading.Thread(target=wechat_second)
    a.start(); b.start()
    a.join(8); b.join(8)
    assert not a.is_alive() and not b.is_alive()
    assert sorted(outcome, key=str) == sorted(["site-reserved", 429], key=str)
