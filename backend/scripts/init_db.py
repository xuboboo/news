"""数据库初始化脚本：创建 pgvector 扩展与全部表（容器启动时自动执行，也可手动运行）。"""
import logging

from app.core.db import Base, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("init-db")


def main():
    from app.models import (  # noqa: F401 确保模型注册（必须全量，漏 import 会静默缺表）
        article, event, report, setting, summary, task_run, usage,
        tenant, user, user_session, audit_log,
    )
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("pgvector extension ready")
    Base.metadata.create_all(engine)
    logger.info("tables created")
    # 存量库升级：create_all 只建新表不加列，新增列在此幂等补齐（与 app/main.py init_db 保持一致）
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS likes BIGINT NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS stats_polled_at TIMESTAMPTZ NULL"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS stats_next_poll_at TIMESTAMPTZ NULL"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS stats_fail_count INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_stats_next_poll_at ON article (stats_next_poll_at)"))
    logger.info("column migration done")


if __name__ == "__main__":
    main()
