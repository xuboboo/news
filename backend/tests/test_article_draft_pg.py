"""PostgreSQL-only article-draft concurrency checks.

Safety: TEST_DATABASE_URL is mandatory for execution, must identify an explicit
PostgreSQL test database, and must not target the configured business database.
Each module run creates one random schema and drops only that schema. No
TRUNCATE, Redis, or network LLM calls are used.
"""
import os
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="requires isolated TEST_DATABASE_URL")
_CONNECT_ARGS = {"connect_timeout": 3, "options": "-c statement_timeout=7000 -c lock_timeout=4000"}


def _same_database(left, right):
    return (left.drivername.split("+")[0] == right.drivername.split("+")[0]
            and (left.host or "").lower() == (right.host or "").lower()
            and left.port == right.port and (left.database or "") == (right.database or ""))


@pytest.fixture()
def pg_env():
    # Fail closed before opening any socket: never fall back to DATABASE_URL.
    url = make_url(TEST_DATABASE_URL)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must use PostgreSQL")
    database = (url.database or "").lower()
    if not database or not any(token in database for token in ("_test", "-test", "test_", "test-")):
        pytest.fail("refusing PG URL without a clear test database-name marker")
    from app.core.config import get_settings
    if _same_database(url, make_url(get_settings().database_url)):
        pytest.fail("refusing TEST_DATABASE_URL that identifies the configured business database")

    schema = "draft_test_" + uuid.uuid4().hex
    admin_engine = create_engine(url, pool_pre_ping=True, connect_args=_CONNECT_ARGS)
    created = False
    engine = None
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        engine = create_engine(url, pool_pre_ping=True, connect_args=_CONNECT_ARGS)
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def set_schema(dbapi_connection, _record):
            was_autocommit = dbapi_connection.autocommit
            try:
                dbapi_connection.autocommit = True
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute(f'SET search_path TO "{schema}"')
                finally:
                    cursor.close()
            finally:
                dbapi_connection.autocommit = was_autocommit

        from app.models.article_draft import ArticleDraft
        from app.models.setting import SystemSetting
        from app.models.audit_log import AuditLog
        ArticleDraft.__table__.create(engine)
        SystemSetting.__table__.create(engine)
        AuditLog.__table__.create(engine)
        yield engine, sessionmaker(bind=engine, expire_on_commit=False), schema
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _timeout_join(thread, seconds=8):
    thread.join(seconds)
    assert not thread.is_alive(), "PostgreSQL concurrency check exceeded bounded timeout"


def _draft(factory, *, status="ready", generation=1):
    from app.models.article_draft import ArticleDraft
    session = factory()
    row = ArticleDraft(source_type="article", source_id=90001, source_title="offline", tier="brief",
                       status=status, gen_count=generation)
    session.add(row)
    session.commit()
    draft_id = row.id
    session.close()
    return draft_id


def _prepare_user_api(monkeypatch, *, limit=0):
    import app.api.v2.drafts as drafts_api
    user = SimpleNamespace(id=21001, email="test-user", tenant_id=None)
    monkeypatch.setattr(drafts_api, "current_user_of", lambda _request: user)
    monkeypatch.setattr(drafts_api.config_service, "get_effective",
                        lambda _db, key: ("true" if key == "article_gen_enabled" else limit))
    return drafts_api, user


def test_same_draft_advisory_lock_excludes_second_worker(pg_env, monkeypatch):
    """真实 PG advisory transaction lock 让同稿第二个 worker 安全退出。"""
    _, factory, _ = pg_env
    import app.services.article_gen_service as service
    draft_id = _draft(factory, status="generating")
    holder = factory()
    outcome, called = [], []
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(hashtext('article_draft:' || :i))"), {"i": draft_id})
        monkeypatch.setattr(service.llm_client, "chat", lambda *a, **k: called.append(1))
        def call_worker():
            session = factory()
            try:
                outcome.append(service.generate_article_for_draft(session, draft_id, generation=1))
            finally:
                session.close()
        worker = threading.Thread(target=call_worker)
        worker.start()
        _timeout_join(worker)
    finally:
        holder.rollback()
        holder.close()
    assert outcome and outcome[0][0] is False
    assert called == []


def test_two_sessions_regenerate_one_draft_once(pg_env, monkeypatch):
    """两个独立 API 会话都读旧快照，只一条 CAS、审计和队列派发成功。"""
    _, factory, _ = pg_env
    from app.models.audit_log import AuditLog
    from app.models.article_draft import ArticleDraft
    drafts_api, _ = _prepare_user_api(monkeypatch)
    draft_id = _draft(factory)
    original_budget = drafts_api._check_hourly_budget
    rendezvous = threading.Barrier(2)

    def synchronized_budget(session, user):
        original_budget(session, user)
        rendezvous.wait(timeout=5)

    monkeypatch.setattr(drafts_api, "_check_hourly_budget", synchronized_budget)
    import app.tasks.tasks as tasks_mod
    dispatches, outcomes = [], []
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *a, **k: dispatches.append((a, k)))

    def attempt():
        session = factory()
        try:
            revision = session.get(ArticleDraft, draft_id).revision
            outcomes.append(drafts_api.regenerate_draft(draft_id, None, {"revision": revision}, session))
        except Exception as exc:
            outcomes.append(exc)
        finally:
            session.close()

    workers = [threading.Thread(target=attempt) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        _timeout_join(worker)
    successes = [value for value in outcomes if isinstance(value, dict)]
    conflicts = [value for value in outcomes if getattr(value, "status_code", None) == 409]
    check = factory()
    audit_count = check.query(AuditLog).filter(AuditLog.action == "draft.regenerated").count()
    row = check.get(ArticleDraft, draft_id)
    check.close()
    assert len(successes) == len(conflicts) == audit_count == len(dispatches) == 1
    assert row.gen_count == 2 and row.status == "generating"


def test_hourly_limit_one_allows_only_one_concurrent_regeneration(pg_env, monkeypatch):
    """PG 两会话同用户竞争每小时额度 1，只一个审计预留和一次派发。"""
    _, factory, _ = pg_env
    from app.models.audit_log import AuditLog
    from app.models.article_draft import ArticleDraft
    drafts_api, _ = _prepare_user_api(monkeypatch, limit=1)
    draft_ids = [_draft(factory), _draft(factory)]
    import app.tasks.tasks as tasks_mod
    dispatches, outcomes = [], []
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *a, **k: dispatches.append((a, k)))
    gate = threading.Barrier(2)

    def attempt(draft_id):
        session = factory()
        try:
            gate.wait(timeout=5)
            revision = session.get(ArticleDraft, draft_id).revision
            outcomes.append(drafts_api.regenerate_draft(draft_id, None, {"revision": revision}, session))
        except Exception as exc:
            outcomes.append(exc)
        finally:
            session.close()

    workers = [threading.Thread(target=attempt, args=(draft_id,)) for draft_id in draft_ids]
    for worker in workers:
        worker.start()
    for worker in workers:
        _timeout_join(worker)
    success = [value for value in outcomes if isinstance(value, dict)]
    limited = [value for value in outcomes if getattr(value, "status_code", None) == 429]
    check = factory()
    audit_count = check.query(AuditLog).filter(AuditLog.action == "draft.regenerated").count()
    check.close()
    assert len(success) == len(limited) == audit_count == len(dispatches) == 1, [repr(value) for value in outcomes]


def test_stale_identity_map_observes_disabled_switch_and_old_generation_exits(pg_env, monkeypatch):
    """跨会话提交关闭值绕过 identity map；旧 generation 不调用模型。"""
    _, factory, _ = pg_env
    from app.models.article_draft import ArticleDraft
    from app.models.setting import SystemSetting
    import app.services.article_gen_service as service
    draft_id = _draft(factory, status="generating", generation=2)
    setup = factory()
    setup.add(SystemSetting(key="article_gen_enabled", value="true"))
    setup.commit()
    setup.close()
    stale_session = factory()
    cached = stale_session.get(SystemSetting, "article_gen_enabled")
    assert cached.value == "true"
    writer = factory()
    writer.get(SystemSetting, "article_gen_enabled").value = "false"
    writer.commit()
    writer.close()
    calls = []
    monkeypatch.setattr(service, "effective_llm", lambda _db: ("https://llm.invalid", "fake", "model", "openai"))
    monkeypatch.setattr(service, "assert_public_http_url", lambda value: value)
    monkeypatch.setattr(service.config_service, "get_effective", lambda _db, _key: "")
    monkeypatch.setattr(service, "_collect_material", lambda _db, _row: ([{"title": "t", "source": "s", "content": "c"}], ""))
    monkeypatch.setattr(service.llm_client, "chat", lambda *a, **k: calls.append(1))
    try:
        assert service.generate_article_for_draft(stale_session, draft_id, generation=2) == (False, "article generation disabled")
        assert service.generate_article_for_draft(stale_session, draft_id, generation=1) == (False, "generation superseded")
    finally:
        stale_session.close()
    assert calls == []
    check = factory()
    row = check.get(ArticleDraft, draft_id)
    assert row.status == "failed" and row.gen_count == 2
    check.close()


def test_generation_deleted_midflight_is_not_resurrected(pg_env, monkeypatch):
    """实际生成期间删除草稿，完成阶段 CAS 不会重建已删除记录。"""
    _, factory, _ = pg_env
    from app.models.article_draft import ArticleDraft
    from app.models.setting import SystemSetting
    import app.services.article_gen_service as service
    draft_id = _draft(factory, status="generating")
    s = factory()
    s.add(SystemSetting(key="article_gen_enabled", value="true"))
    s.commit()
    s.close()
    monkeypatch.setattr(service, "effective_llm", lambda _db: ("https://llm.invalid", "fake", "test-model", "openai"))
    monkeypatch.setattr(service, "assert_public_http_url", lambda value: value)
    monkeypatch.setattr(service.config_service, "get_effective", lambda _db, _key: "")
    monkeypatch.setattr(service, "_collect_material", lambda _db, _row: ([{"title": "t", "source": "s", "content": "c"}], ""))
    monkeypatch.setattr(service, "record_usage", lambda **_kwargs: None)

    def delete_during_call(*_args, **_kwargs):
        other = factory()
        try:
            other.delete(other.get(ArticleDraft, draft_id))
            other.commit()
        finally:
            other.close()
        return ('{"title":"标题","digest":"摘要","content_md":"正文"}', {})

    monkeypatch.setattr(service.llm_client, "chat", delete_during_call)
    generation_session = factory()
    try:
        outcome = service.generate_article_for_draft(generation_session, draft_id, generation=1)
    finally:
        generation_session.close()
    assert outcome == (False, "generation superseded")
    check = factory()
    assert check.get(ArticleDraft, draft_id) is None
    check.close()


def test_failure_recovery_obeys_live_pg_owner_and_generation_fence(pg_env, monkeypatch):
    """Recovery cannot steal a live generator's advisory lock; free old generations CAS safely."""
    _, factory, _ = pg_env
    import app.services.article_draft_recovery as recovery
    from app.models.article_draft import ArticleDraft

    monkeypatch.setattr(recovery, "new_session", factory)
    draft_id = _draft(factory, status="generating", generation=1)
    owner = factory()
    owner.execute(text("SELECT pg_advisory_xact_lock(hashtext('article_draft:' || :i))"), {"i": draft_id})
    try:
        assert not recovery.mark_failed_if_unowned(draft_id, 1, "duplicate exhausted")
        check = factory()
        row = check.get(ArticleDraft, draft_id)
        assert row.status == "generating" and row.gen_count == 1
        check.close()
    finally:
        owner.rollback()
        owner.close()

    assert recovery.mark_failed_if_unowned(draft_id, 1, "owner lost")
    check = factory()
    row = check.get(ArticleDraft, draft_id)
    assert row.status == "failed" and row.gen_count == 1
    check.close()

    newer_id = _draft(factory, status="generating", generation=2)
    assert not recovery.mark_failed_if_unowned(newer_id, 1, "old generation")
    check = factory()
    row = check.get(ArticleDraft, newer_id)
    assert row.status == "generating" and row.gen_count == 2
    check.close()
