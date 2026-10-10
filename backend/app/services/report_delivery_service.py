"""Transactional outbox and retry loop for daily report webhook deliveries."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.report_delivery import ReportDelivery
from app.services.report_push import push_targets, send_report_target

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
SENDING_STALE_AFTER = timedelta(minutes=20)


def _content_hash(content: dict) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enqueue_report_deliveries(db: Session, report_date, content: dict) -> int:
    """Add one pending row per configured provider in the caller's transaction.

    The caller commits the report and outbox rows together. A unique idempotency key
    prevents re-generating the same date/content from scheduling duplicate sends.
    """
    # Persist a detached JSON value rather than retaining references into the caller's
    # report object; retries must send exactly the content that was enqueued.
    snapshot = json.loads(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str))
    digest = _content_hash(snapshot)
    targets = push_targets(db)
    created = 0
    for provider, _url, _secret in targets:
        key = hashlib.sha256(f"{report_date.isoformat()}:{provider}:{digest}".encode()).hexdigest()
        exists = db.query(ReportDelivery.id).filter_by(idempotency_key=key).first()
        if exists:
            continue
        db.add(ReportDelivery(
            report_date=report_date,
            channel=provider,
            idempotency_key=key,
            payload_hash=digest,
            content_snapshot=json.loads(json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
            status="pending",
            attempts=0,
        ))
        created += 1
    db.flush()
    return created


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(6 * 60 * 60, 30 * (2 ** max(0, attempts - 1))))


def process_due_deliveries(db: Session, limit: int = 20) -> dict:
    """Claim due rows with SKIP LOCKED, send outside the claim transaction, then persist outcomes."""
    now = datetime.now(timezone.utc)
    stale_before = now - SENDING_STALE_AFTER
    due = or_(
        ((ReportDelivery.status.in_(("pending", "retry_wait"))) & (ReportDelivery.next_attempt_at <= now)),
        ((ReportDelivery.status == "sending") & (ReportDelivery.updated_at <= stale_before)),
    )
    rows = (
        db.query(ReportDelivery)
        .filter(due)
        .order_by(ReportDelivery.next_attempt_at, ReportDelivery.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    ids = [row.id for row in rows]
    for row in rows:
        row.status = "sending"
        row.attempts += 1
        row.updated_at = now
    db.commit()

    sent = failed = dead = 0
    for delivery_id in ids:
        delivery = db.get(ReportDelivery, delivery_id)
        if delivery is None:
            continue
        channel = delivery.channel
        content_snapshot = json.loads(json.dumps(
            delivery.content_snapshot, ensure_ascii=False, sort_keys=True, default=str,
        ))
        target = next((item for item in push_targets(db) if item[0] == channel), None)
        # Copy every ORM value before rollback: rollback expires loaded instances, and
        # re-reading one after this point would start a DB transaction during HTTP.
        db.rollback()
        if target is None:
            ok, detail = False, "channel is not configured or did not pass URL validation"
        else:
            provider, url, secret = target
            ok, detail = send_report_target(provider, url, secret, content_snapshot)
        delivery = db.get(ReportDelivery, delivery_id)
        if delivery is None:
            db.rollback()
            continue
        delivery.updated_at = datetime.now(timezone.utc)
        if ok:
            delivery.status = "sent"
            delivery.last_error = None
            delivery.delivered_at = delivery.updated_at
            sent += 1
        else:
            delivery.last_error = detail[:400]
            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.status = "dead"
                dead += 1
            else:
                delivery.status = "retry_wait"
                delivery.next_attempt_at = delivery.updated_at + _retry_delay(delivery.attempts)
                failed += 1
            logger.warning("report delivery failed id=%s channel=%s attempt=%s: %s",
                           delivery.id, delivery.channel, delivery.attempts, detail[:160])
        db.commit()
    return {"claimed": len(ids), "sent": sent, "retry_wait": failed, "dead": dead}
