"""Fail-closed lock behavior for non-idempotent background work."""
from contextlib import contextmanager
from types import SimpleNamespace

from app.utils import lock as lock_mod


def test_acquire_lock_result_distinguishes_busy_and_unavailable(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(lock_mod, "_client", lambda: fake)

    acquired = lock_mod.acquire_lock_result("result-test", 1000)
    busy = lock_mod.acquire_lock_result("result-test", 1000)
    assert acquired.state == lock_mod.LockState.ACQUIRED
    assert acquired.token
    assert busy.state == lock_mod.LockState.BUSY
    assert busy.token is None

    monkeypatch.setattr(lock_mod, "_client", lambda: DeadRedis())
    unavailable = lock_mod.acquire_lock_result("result-test", 1000)
    assert unavailable.state == lock_mod.LockState.UNAVAILABLE
    assert unavailable.token is None


def test_stats_task_fails_closed_when_redis_lock_is_unavailable(monkeypatch):
    import app.tasks.tasks as tasks_mod
    from app.services import config_service
    from app.utils import lock as lock_module

    class FakeDB:
        def close(self):
            pass

    db = FakeDB()
    monkeypatch.setattr(tasks_mod, "new_session", lambda: db)
    monkeypatch.setattr(config_service, "get_effective", lambda _db, _key: True)
    monkeypatch.setattr(
        lock_module,
        "acquire_lock_result",
        lambda *_args: lock_module.LockResult(lock_module.LockState.UNAVAILABLE),
    )

    import app.crawler.stats as stats_mod

    monkeypatch.setattr(stats_mod, "poll_due_articles", lambda _db: (_ for _ in ()).throw(AssertionError("poll must not run")))
    runs = []

    @contextmanager
    def fake_record_task_run(name, trigger="auto"):
        run = SimpleNamespace(status="running", detail="")
        runs.append((name, trigger, run))
        yield run

    monkeypatch.setattr(tasks_mod, "record_task_run", fake_record_task_run)

    result = tasks_mod.stats_task(trigger="manual")
    assert result == {"skipped": True, "reason": "redis unavailable (fail-closed)"}
    assert len(runs) == 1
    name, trigger, run = runs[0]
    assert (name, trigger) == ("stats", "manual")
    assert run.status == "degraded"
    assert run.detail == "skipped: redis unavailable (fail-closed)"


def test_event_task_fails_closed_when_redis_lock_is_unavailable(monkeypatch):
    import app.tasks.tasks as tasks_mod
    from app.utils import lock as lock_module

    monkeypatch.setattr(
        lock_module,
        "acquire_lock_result",
        lambda *_args: lock_module.LockResult(lock_module.LockState.UNAVAILABLE),
    )
    monkeypatch.setattr(
        tasks_mod,
        "run_event_clustering",
        lambda _db: (_ for _ in ()).throw(AssertionError("clustering must not run")),
    )
    runs = []

    @contextmanager
    def fake_record_task_run(name, trigger="auto"):
        run = SimpleNamespace(status="running", detail="")
        runs.append((name, trigger, run))
        yield run

    monkeypatch.setattr(tasks_mod, "record_task_run", fake_record_task_run)

    result = tasks_mod.event_task(trigger="manual")
    assert result == {"skipped": True, "reason": "redis unavailable (fail-closed)"}
    assert len(runs) == 1
    name, trigger, run = runs[0]
    assert (name, trigger) == ("event", "manual")
    assert run.status == "degraded"
    assert run.detail == "skipped: redis unavailable (fail-closed)"


class FakeRedis:
    """Minimal Redis substitute for the lock result state test."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, px=None):  # noqa: ANN001, ARG002
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class DeadRedis:
    def set(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")
