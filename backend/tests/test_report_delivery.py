"""Transactional outbox tests use SQLite and stub only the external webhook."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.report_delivery import ReportDelivery
from app.services import report_delivery_service as delivery_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    ReportDelivery.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()
    engine.dispose()


def test_enqueue_is_idempotent_and_keeps_content_snapshot(db, monkeypatch):
    monkeypatch.setattr(
        delivery_service,
        "push_targets",
        lambda _db: [("wecom", "https://example.test/hook", "")],
    )
    content = {"date": "2026-10-10", "overview": "早报内容", "generated_at": "2026-10-10T00:00:00Z"}

    assert delivery_service.enqueue_report_deliveries(db, date(2026, 10, 10), content) == 1
    content["overview"] = "调用后修改"
    db.commit()
    assert delivery_service.enqueue_report_deliveries(
        db,
        date(2026, 10, 10),
        {"date": "2026-10-10", "overview": "早报内容", "generated_at": "2026-10-10T00:00:00Z"},
    ) == 0

    row = db.query(ReportDelivery).one()
    assert row.status == "pending"
    assert row.content_snapshot["overview"] == "早报内容"
    assert row.channel == "wecom"
    assert "https://" not in str(row.content_snapshot)


def test_due_delivery_persists_retry_then_success(db, monkeypatch):
    def configured_targets(session):
        # Simulate DB-backed config lookup, which opens a transaction in production.
        session.execute(text("SELECT 1"))
        return [("feishu", "https://example.test/hook", "secret-not-stored")]

    monkeypatch.setattr(delivery_service, "push_targets", configured_targets)
    outcomes = iter([(False, "provider rejected message: code=1"), (True, "ok")])
    sent_calls = []

    def send(provider, url, secret, content):
        assert not db.in_transaction(), "HTTP delivery must happen outside a DB transaction"
        sent_calls.append((provider, url, secret, content))
        return next(outcomes)

    monkeypatch.setattr(delivery_service, "send_report_target", send)
    snapshot = {"date": "2026-10-10", "overview": "早报"}
    row = ReportDelivery(
        report_date=date(2026, 10, 10),
        channel="feishu",
        idempotency_key="a" * 64,
        payload_hash="b" * 64,
        content_snapshot=snapshot,
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(row)
    db.commit()

    first = delivery_service.process_due_deliveries(db)
    db.refresh(row)
    assert first == {"claimed": 1, "sent": 0, "retry_wait": 1, "dead": 0}
    assert row.status == "retry_wait"
    assert row.attempts == 1
    assert row.last_error == "provider rejected message: code=1"
    assert sent_calls == [(
        "feishu",
        "https://example.test/hook",
        "secret-not-stored",
        snapshot,
    )]

    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = delivery_service.process_due_deliveries(db)
    db.refresh(row)
    assert second == {"claimed": 1, "sent": 1, "retry_wait": 0, "dead": 0}
    assert row.status == "sent"
    assert row.attempts == 2
    assert row.delivered_at is not None
    assert row.last_error is None
    assert sent_calls == [
        ("feishu", "https://example.test/hook", "secret-not-stored", snapshot),
        ("feishu", "https://example.test/hook", "secret-not-stored", snapshot),
    ]


def test_delivery_stops_after_maximum_attempts(db, monkeypatch):
    monkeypatch.setattr(
        delivery_service,
        "push_targets",
        lambda _db: [("wecom", "https://example.test/hook", "")],
    )
    monkeypatch.setattr(delivery_service, "send_report_target", lambda *_args: (False, "HTTP 503"))
    row = ReportDelivery(
        report_date=date(2026, 10, 10),
        channel="wecom",
        idempotency_key="c" * 64,
        payload_hash="d" * 64,
        content_snapshot={"date": "2026-10-10"},
        status="retry_wait",
        attempts=delivery_service.MAX_ATTEMPTS - 1,
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(row)
    db.commit()

    result = delivery_service.process_due_deliveries(db)
    db.refresh(row)
    assert result == {"claimed": 1, "sent": 0, "retry_wait": 0, "dead": 1}
    assert row.status == "dead"
    assert row.attempts == delivery_service.MAX_ATTEMPTS


def test_report_delivery_task_caps_sequential_http_batch(monkeypatch):
    from app.tasks import tasks

    class FakeSession:
        closed = False

        def close(self):
            self.closed = True

    session = FakeSession()
    calls = []
    monkeypatch.setattr(tasks, "new_session", lambda: session)
    monkeypatch.setattr(
        delivery_service,
        "process_due_deliveries",
        lambda db, limit: calls.append((db, limit)) or {
            "claimed": 0, "sent": 0, "retry_wait": 0, "dead": 0,
        },
    )

    assert tasks.report_delivery_task() == {
        "claimed": 0, "sent": 0, "retry_wait": 0, "dead": 0,
    }
    assert calls == [(session, 5)]
    assert session.closed


def test_celery_schedules_due_report_deliveries():
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["report-delivery-every-minute"]
    assert entry["task"] == "report_delivery_task"
