"""Generation-failure recovery and Celery signal safety tests."""
from types import SimpleNamespace

import pytest
from billiard.exceptions import WorkerLostError
from celery.exceptions import MaxRetriesExceededError

import app.tasks.tasks as tasks_mod
from app.models.article_draft import ArticleDraft
from app.services import article_draft_recovery as recovery


def _draft(db, *, status="generating", generation=1):
    session = db.factory()
    row = ArticleDraft(source_type="article", source_id=1, source_title="fixture",
                       tier="brief", status=status, gen_count=generation)
    session.add(row)
    session.commit()
    draft_id = row.id
    session.close()
    return draft_id


def _status(db, draft_id):
    session = db.factory()
    row = session.get(ArticleDraft, draft_id)
    result = None if row is None else (row.status, row.gen_count, row.error)
    session.close()
    return result


def test_failure_recovery_is_generation_and_status_fenced(db, monkeypatch):
    monkeypatch.setattr(recovery, "new_session", db.factory)
    draft_id = _draft(db)
    assert recovery.mark_failed_if_unowned(draft_id, 1, "lost")
    assert _status(db, draft_id) == ("failed", 1, "lost")

    newer = _draft(db, generation=2)
    assert not recovery.mark_failed_if_unowned(newer, 1, "old task")
    assert _status(db, newer) == ("generating", 2, "")
    ready = _draft(db, status="ready")
    assert not recovery.mark_failed_if_unowned(ready, 1, "lost")
    assert _status(db, ready) == ("ready", 1, "")
    deleted = _draft(db)
    session = db.factory()
    session.delete(session.get(ArticleDraft, deleted)); session.commit(); session.close()
    assert not recovery.mark_failed_if_unowned(deleted, 1, "lost")


def test_active_owner_lock_prevents_failure_transition(db, monkeypatch):
    class LockUnavailableSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def __init__(self): self.did_query = False
        def execute(self, *_args, **_kwargs):
            self.did_query = True
            return SimpleNamespace(scalar=lambda: False)
        def rollback(self): pass
        def close(self): pass
    fake = LockUnavailableSession()
    monkeypatch.setattr(recovery, "new_session", lambda: fake)
    assert not recovery.mark_failed_if_unowned(42, 1, "retry exhausted")
    assert fake.did_query


def test_worker_lost_signal_only_recovers_explicit_article_task(db, monkeypatch):
    calls = []
    monkeypatch.setattr(recovery, "recover_failed_generation", lambda *a: calls.append(a))
    tasks_mod._article_generation_worker_lost(
        sender=tasks_mod.article_gen_task, task_id="id-1",
        exception=WorkerLostError("child killed"), args=(19,),
        kwargs={"generation": 3},
    )
    assert calls == [(19, 3, "文章生成子进程意外退出（Celery WorkerLostError，task_id=id-1）")]
    calls.clear()
    tasks_mod._article_generation_worker_lost(
        sender=SimpleNamespace(name="summary_task"), exception=WorkerLostError("lost"),
        args=(19,), kwargs={"generation": 3},
    )
    tasks_mod._article_generation_worker_lost(
        sender=tasks_mod.article_gen_task, exception=RuntimeError("bug"),
        args=(19,), kwargs={"generation": 3},
    )
    tasks_mod._article_generation_worker_lost(
        sender=tasks_mod.article_gen_task, exception=WorkerLostError("lost"),
        args=(19,), kwargs={},
    )
    assert calls == []


def test_signal_recovery_database_exception_does_not_escape(monkeypatch):
    def broken(*_args): raise RuntimeError("isolated DB unavailable")
    monkeypatch.setattr(recovery, "mark_failed_if_unowned", broken)
    monkeypatch.setattr(recovery, "schedule_failure_confirmation", lambda *_a: None)
    recovery.recover_failed_generation(1, 1, "lost")


def test_retry_exhaustion_marks_only_when_no_owner_and_schedules_check(db, monkeypatch):
    monkeypatch.setattr(tasks_mod, "new_session", db.factory)
    import app.services.article_gen_service as gen_service
    monkeypatch.setattr(gen_service, "generate_article_for_draft",
                        lambda *_a, **_k: (False, "another generation in flight"))
    retry_errors = []
    monkeypatch.setattr(tasks_mod.article_gen_task, "retry",
                        lambda **_kw: (_ for _ in ()).throw(MaxRetriesExceededError("spent")))
    monkeypatch.setattr(recovery, "recover_failed_generation", lambda *a: retry_errors.append(a))
    # Task call's short DB session is SQLite-backed in this test.
    draft_id = _draft(db)
    with pytest.raises(MaxRetriesExceededError):
        tasks_mod.article_gen_task(draft_id, generation=1)
    assert retry_errors == [(draft_id, 1, "同稿生成锁等待重试次数已耗尽，请重新生成")]


def test_unexpected_crash_uses_owner_safe_recovery(db, monkeypatch):
    import app.services.article_gen_service as gen_service

    monkeypatch.setattr(tasks_mod, "new_session", db.factory)
    monkeypatch.setattr(gen_service, "generate_article_for_draft",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unexpected")))
    calls = []
    monkeypatch.setattr(recovery, "recover_failed_generation", lambda *args: calls.append(args))
    draft_id = _draft(db)
    with pytest.raises(RuntimeError, match="unexpected"):
        tasks_mod.article_gen_task(draft_id, generation=1)
    assert calls == [(draft_id, 1, "生成任务异常：unexpected")]
    assert _status(db, draft_id) == ("generating", 1, "")


def test_delayed_confirmation_schedule_and_ai_route(monkeypatch):
    from app.tasks.celery_app import celery_app

    published = []
    monkeypatch.setattr(
        tasks_mod.article_gen_failure_confirm_task, "apply_async",
        lambda **kwargs: published.append(kwargs),
    )
    recovery.schedule_failure_confirmation(42, 3, "lock owner unknown")
    assert published == [{"args": (42, 3, "lock owner unknown"), "countdown": 3605}]
    route = celery_app.amqp.router.route({}, "article_gen_failure_confirm_task")
    assert route["queue"].name == "ai"
    assert not celery_app.conf.task_reject_on_worker_lost


def test_confirmation_is_one_shot_and_generation_fenced(db, monkeypatch):
    monkeypatch.setattr(recovery, "new_session", db.factory)
    draft_id = _draft(db, generation=2)
    monkeypatch.setattr(tasks_mod, "mark_failed_if_unowned", recovery.mark_failed_if_unowned, raising=False)
    result = tasks_mod.article_gen_failure_confirm_task(draft_id, 1, "old")
    assert result["marked_failed"] is False
    assert _status(db, draft_id) == ("generating", 2, "")
    result = tasks_mod.article_gen_failure_confirm_task(draft_id, 2, "confirmed")
    assert result["marked_failed"] is True
    assert _status(db, draft_id) == ("failed", 2, "confirmed")
