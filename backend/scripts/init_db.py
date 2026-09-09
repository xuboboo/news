"""数据库初始化脚本：创建 pgvector 扩展与全部表（可手动运行；容器启动自动执行的是
app/main.py 的 init_db，两者幂等 SQL 保持一致，改任一侧须同步另一侧）。"""
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
        # 标题 trgm GIN 索引依赖 pg_trgm 扩展：缺了它下方索引创建直接报错
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        logger.info("pgvector / pg_trgm extensions ready")
    Base.metadata.create_all(engine)
    logger.info("tables created")
    # 存量库升级：create_all 只建新表不加列，新增列在此幂等补齐（与 app/main.py init_db 保持一致）
    with engine.begin() as conn:
        # 标题模糊搜索（ILIKE '%kw%'）走 trgm GIN 索引，避免全表顺序扫描
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_article_title_trgm ON article USING gin (title gin_trgm_ops)"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS likes BIGINT NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS stats_polled_at TIMESTAMPTZ NULL"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS stats_next_poll_at TIMESTAMPTZ NULL"))
        conn.execute(text("ALTER TABLE article ADD COLUMN IF NOT EXISTS stats_fail_count INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_stats_next_poll_at ON article (stats_next_poll_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_log_user_id ON audit_log (user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_log_tenant_id ON audit_log (tenant_id)"))
        # 审计防篡改哈希链（2026-09-09）：存量库补列，存量行 row_hash='' 视为链前遗留段
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS prev_hash VARCHAR(64) NOT NULL DEFAULT ''"))
        conn.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS row_hash VARCHAR(64) NOT NULL DEFAULT ''"))
        # 成本看板（2026-09-09）：价目缓存价 + 明细按任务×时间复合索引（与 app/main.py init_db 保持一致）
        conn.execute(text("ALTER TABLE model_price ADD COLUMN IF NOT EXISTS price_cache NUMERIC(12, 4) NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_usage_log_task_created ON ai_usage_log (task_type, created_at)"))
    logger.info("column migration done")


if __name__ == "__main__":
    main()
