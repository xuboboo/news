"""Fail-closed guard for the disposable article-queue test harness (stdlib only)."""
import os
from urllib.parse import urlsplit


def validate():
    if os.environ.get("DRAFT_QUEUE_CHECK") != "1":
        raise SystemExit("refusing to run: set DRAFT_QUEUE_CHECK=1 explicitly")
    prefix = os.environ.get("DRAFT_QUEUE_PREFIX", "")
    if not prefix.startswith("draft-queue-check-") or len(prefix) < 24:
        raise SystemExit("refusing to run: invalid DRAFT_QUEUE_PREFIX")
    if os.environ.get("ENVIRONMENT", "").lower() != "test":
        raise SystemExit("refusing to run: ENVIRONMENT must be test")
    db = urlsplit(os.environ.get("DATABASE_URL", ""))
    redis = urlsplit(os.environ.get("REDIS_URL", ""))
    if db.scheme not in ("postgresql", "postgresql+psycopg"):
        raise SystemExit("refusing to run: DATABASE_URL must be PostgreSQL")
    if db.hostname != prefix + "-pg" or db.path.lstrip("/") != "draft_queue_test":
        raise SystemExit("refusing to run: DATABASE_URL host/database is not this test instance")
    if redis.scheme != "redis" or redis.hostname != prefix + "-redis" or redis.port != 6379:
        raise SystemExit("refusing to run: REDIS_URL is not this test instance")
    if os.environ.get("LLM_BASE_URL") != "https://mock.invalid/v1" or os.environ.get("LLM_API_KEY") != "offline":
        raise SystemExit("refusing to run: live LLM configuration is forbidden")
    for key in ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "REPORT_PUSH_FEISHU_WEBHOOK_URL", "REPORT_PUSH_WECOM_WEBHOOK_URL", "ALERT_WEBHOOK_URL"):
        if os.environ.get(key, ""):
            raise SystemExit("refusing to run: external integration must be empty: " + key)
    return prefix


PREFIX = validate()
