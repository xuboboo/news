"""数据库初始化脚本：创建 pgvector 扩展与全部表（容器启动时自动执行，也可手动运行）。"""
import logging

from app.core.db import Base, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("init-db")


def main():
    from app.models import (  # noqa: F401 确保模型注册（必须全量，漏 import 会静默缺表）
        article, event, report, setting, summary, task_run, usage
    )
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("pgvector extension ready")
    Base.metadata.create_all(engine)
    logger.info("tables created")


if __name__ == "__main__":
    main()
