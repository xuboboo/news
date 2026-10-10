"""Fail-closed URL validation for PostgreSQL integration test targets."""
from types import SimpleNamespace

import pytest

from pg_safety import validate_test_database_url


_BUSINESS_URL = "postgresql+psycopg://business:secret@db.example:5432/radar_business"
_SAFE_TEST_URL = "postgresql+psycopg://tester:secret@db.example:5432/radar_test"


def test_accepts_explicit_isolated_postgres_test_database(monkeypatch):
    import app.core.config

    monkeypatch.setattr(app.core.config, "get_settings", lambda: SimpleNamespace(database_url=_BUSINESS_URL))
    url = validate_test_database_url(_SAFE_TEST_URL)
    assert url.database == "radar_test"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///radar_test.db",
        "postgresql+psycopg://tester:secret@db.example:5432/postgres",
        "postgresql+psycopg://tester:secret@db.example:5432/radar_business",
        "postgresql+psycopg://tester:secret@db.example:5432/radar",
    ],
)
def test_rejects_non_test_or_business_database_urls(monkeypatch, url):
    import app.core.config

    monkeypatch.setattr(app.core.config, "get_settings", lambda: SimpleNamespace(database_url=_BUSINESS_URL))
    with pytest.raises(ValueError):
        validate_test_database_url(url)


def test_rejects_business_database_even_when_host_alias_differs(monkeypatch):
    import app.core.config

    business = "postgresql+psycopg://business:secret@localhost:5432/radar_test"
    monkeypatch.setattr(app.core.config, "get_settings", lambda: SimpleNamespace(database_url=business))
    candidate = "postgresql+psycopg://tester:secret@127.0.0.1/radar_test"
    with pytest.raises(ValueError, match="business database"):
        validate_test_database_url(candidate)


def test_requires_database_url(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="required"):
        validate_test_database_url(None)
