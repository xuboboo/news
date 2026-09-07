"""FastAPI 入口。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v2.admin import router as admin_router
from app.api.v2.routes import router
from app.core.config import get_settings
from app.core.db import Base, engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def init_db() -> None:
    """建库：扩展 + 全部表 + 检索索引。

    显式 import 全部模型模块——create_all 只建"已注册"的表，
    漏 import 会静默缺表（system_setting/task_run 曾因此仅靠间接 import 才建上）。
    """
    from app.models import article, event, report, setting, summary, task_run, usage  # noqa: F401 确保模型注册

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # 标题模糊搜索（ILIKE '%kw%'）走 trgm GIN 索引，避免全表顺序扫描
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_article_title_trgm ON article USING gin (title gin_trgm_ops)")
        )
    Base.metadata.create_all(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.admin_no_auth:
        logging.getLogger("uvicorn.error").warning(
            "ADMIN_NO_AUTH=true：管理面板接口（/api/v2/admin/*）无需 Token 即可访问，"
            "存在密钥泄露与被任意网页跨域调用的风险；公网部署必须设为 false"
        )
    yield


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    description="华军资讯雷达 V2.0 — 科技情报分析平台后端（FastAPI + PostgreSQL/pgvector + Redis + Celery）",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=False,  # 鉴权走自定义 Header，无需凭据；与通配 origin 组合违反 CORS 规范
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["X-Admin-Token", "Content-Type"],
)

app.include_router(router)
app.include_router(admin_router)


@app.get("/")
def root():
    return {"name": settings.app_name, "version": settings.version, "docs": "/docs"}
