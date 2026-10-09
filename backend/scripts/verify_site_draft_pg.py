"""仅在显式命名的独立 PostgreSQL 测试库中跑草稿 PG 专项；不输出连接凭据。"""
import os

from sqlalchemy.engine import make_url

from app.core.config import get_settings


def main() -> int:
    name = os.environ.get("SITE_PG_TEST_DATABASE", "").strip()
    source = make_url(get_settings().database_url)
    if not (name.startswith("hjradar_site_test_") and source.drivername.startswith("postgresql")):
        raise SystemExit("refusing: requires explicit isolated site test database")
    if source.database == name:
        raise SystemExit("refusing business database")
    os.environ["TEST_DATABASE_URL"] = source.set(database=name).render_as_string(hide_password=False)
    import pytest
    return pytest.main(["tests/test_site_article_draft_pg.py", "tests/test_article_draft_pg.py", "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
