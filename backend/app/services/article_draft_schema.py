"""草稿表增量迁移，启动与手动初始化共用一份幂等 SQL。"""
from sqlalchemy import text


def upgrade_article_draft_columns(conn) -> None:
    # 旧记录仍属于公众号；先补列再建频道索引，不能改写现有草稿。
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS source_refs JSONB NOT NULL DEFAULT '[]'::jsonb"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS channel VARCHAR(16) NOT NULL DEFAULT 'wechat'"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_draft_channel ON article_draft (channel)"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS seo_keywords VARCHAR(160) NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS site_tags VARCHAR(120) NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS cover_guidance VARCHAR(240) NOT NULL DEFAULT ''"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS software_id INTEGER NOT NULL DEFAULT 0"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS is_recommended BOOLEAN NOT NULL DEFAULT FALSE"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS is_headline BOOLEAN NOT NULL DEFAULT FALSE"))
    conn.execute(text("ALTER TABLE article_draft ADD COLUMN IF NOT EXISTS publish_at TIMESTAMPTZ NULL"))
