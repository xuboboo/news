"""Fail-closed guards for PostgreSQL-only tests.

These checks run before opening a connection or destructive SQL.  A test URL must
be an explicit, isolated database and may never equal the application database.
"""
import os
from sqlalchemy.engine import URL, make_url


def validate_test_database_url(raw: str | None = None) -> URL:
    value = raw if raw is not None else os.getenv("TEST_DATABASE_URL")
    if not value:
        raise ValueError("TEST_DATABASE_URL is required for PostgreSQL tests")
    url = make_url(value)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("TEST_DATABASE_URL must use PostgreSQL")
    database = (url.database or "").strip().lower()
    if not database:
        raise ValueError("TEST_DATABASE_URL must include an explicit database name")
    # Never permit known business/system databases, even if someone names a URL
    # with a misleading host or port.
    forbidden = {"hjradar", "postgres", "template0", "template1"}
    if database in forbidden:
        raise ValueError(f"refusing unsafe PostgreSQL database: {database}")
    if not any(marker in database for marker in ("_test", "-test", "test_", "test-")):
        raise ValueError("refusing PG URL without a clear test database-name marker")
    from app.core.config import get_settings
    business = make_url(get_settings().database_url)
    business_database = (business.database or "").strip().lower()
    same_database_name = bool(database and database == business_database)
    if value == os.getenv("DATABASE_URL") or same_database_name or _same_database(url, business):
        raise ValueError("TEST_DATABASE_URL must not identify DATABASE_URL/business database")
    return url


def _same_database(left: URL, right: URL) -> bool:
    return (
        left.drivername.split("+")[0] == right.drivername.split("+")[0]
        and (left.host or "").lower() == (right.host or "").lower()
        and left.port == right.port
        and (left.database or "") == (right.database or "")
    )
