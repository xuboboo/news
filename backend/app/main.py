"""FastAPI 入口。"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v2 import audit, public, users
from app.api.v2.admin import router as admin_router
from app.api.v2.auth import router as auth_router
from app.api.v2.routes import router
from app.auth.deps import require_user
from app.core.config import get_settings
from app.core.db import Base, SessionLocal, engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def init_db() -> None:
    """建库：扩展 + 全部表 + 检索索引。

    显式 import 全部模型模块——create_all 只建"已注册"的表，
    漏 import 会静默缺表（system_setting/task_run 曾因此仅靠间接 import 才建上）。
    """
    from app.models import (  # noqa: F401 确保模型注册
        article,
        event,
        report,
        setting,
        summary,
        task_run,
        usage,
        tenant,
        user,
        user_session,
        audit_log,
    )

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
    # 存量库升级：create_all 只建新表不加列，新增列在此幂等补齐（须在 create_all 之后，新库才有表）
    with engine.begin() as conn:
        # 标题模糊搜索（ILIKE '%kw%'）走 trgm GIN 索引，避免全表顺序扫描（须在 create_all 之后，新库才有表）
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
        # 成本看板（2026-09-09）：价目缓存价（NULL=按输入价全价计）+ 明细按任务×时间复合索引
        conn.execute(text("ALTER TABLE model_price ADD COLUMN IF NOT EXISTS price_cache NUMERIC(12, 4) NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_usage_log_task_created ON ai_usage_log (task_type, created_at)"))


def bootstrap_admin() -> None:
    """首启引导：user 表为空且配置了 BOOTSTRAP_ADMIN_* 时，建默认租户 + 平台超管。

    初始密码标记 must_change_password，首次登录强制修改；建号后可从 .env 移除这两个键。
    """
    from app.auth import passwords
    from app.models.tenant import Tenant
    from app.models.user import ROLE_SUPER_ADMIN, User
    from sqlalchemy import func

    log = logging.getLogger("uvicorn.error")
    with SessionLocal() as db:
        if (db.query(func.count(User.id)).scalar() or 0) > 0:
            return
        email = (settings.bootstrap_admin_email or "").strip().lower()
        password = settings.bootstrap_admin_password or ""
        if not email or not password:
            log.warning(
                "尚无任何账号：请在 backend/.env 设置 BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD "
                "后重启后端完成引导；或执行 docker compose exec backend python scripts/manage_user.py create-super <邮箱> <密码>"
            )
            return
        # get-or-create：user 表被清空但"默认租户"仍在时，重名硬插会 IntegrityError
        # 令启动失败（unique 约束），复用既有租户即可
        tenant = db.query(Tenant).filter_by(name="默认租户").first()
        if tenant is None:
            tenant = Tenant(name="默认租户", remark="引导自动创建")
            db.add(tenant)
            db.flush()
        admin = User(
            email=email,
            password_hash=passwords.hash_password(password),
            display_name="平台超管",
            tenant_id=tenant.id,
            role=ROLE_SUPER_ADMIN,
            must_change_password=True,
            created_by="bootstrap",
        )
        db.add(admin)
        db.commit()
        log.info(f"已创建引导超管 {email}（首次登录需修改密码）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_admin()
    if settings.admin_no_auth:
        logging.getLogger("uvicorn.error").warning(
            "ADMIN_NO_AUTH=true：管理面板接口（/api/v2/admin/*）无需任何鉴权即可访问，"
            "存在密钥泄露与被任意网页跨域调用的风险；公网部署必须设为 false"
        )
    yield


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    description="华军软件园资讯雷达系统 V2.0 后端（FastAPI + PostgreSQL/pgvector + Redis + Celery）",
    # ENABLE_DOCS=false（公网部署建议）：不对外暴露 API 全貌
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,  # 登录会话走 Cookie；origin 保持显式白名单，禁止 *
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["X-Admin-Token", "X-Requested-With", "Content-Type"],
)


@app.exception_handler(HTTPException)
async def _audit_access_denied(request: Request, exc: HTTPException):
    """管理面 403 越权留痕（OWASP：authorization failures 必须审计）。

    member/tenant_admin 探测 /api/v2/admin/* 一律 403，原先不留任何痕迹；
    现记 auth.access_denied（同 操作者+路径 5 分钟去重，防刷表）。
    其余状态码/路径走默认语义不变。
    """
    if exc.status_code == 403 and request.url.path.startswith("/api/v2/admin/"):
        try:
            from app.services import audit_service

            with SessionLocal() as db:
                audit_service.record_access_denied(
                    db, request=request, path=request.url.path, method=request.method
                )
        except Exception:  # noqa: BLE001 审计失败不影响原响应
            logging.getLogger("uvicorn.error").exception("审计 403 落库失败")
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers,
    )

# 公开探针（/api/v2/health）——全站门禁的例外，新增公开端点必须放 public.py
app.include_router(public.router)
# 业务数据接口：全站强制登录（含 stats/热榜/最新/文章/事件/日报 + 运维触发）
app.include_router(router, dependencies=[Depends(require_user)])
# 认证：登录/登出/me/改密/我的会话
app.include_router(auth_router)
# 租户与用户管理（内部按角色鉴权）
app.include_router(users.router)
# 审计日志查询（超管）
app.include_router(audit.router)
# 运维监控面板（require_admin：super 会话 或 X-Admin-Token 后门）
app.include_router(admin_router)


@app.get("/")
def root():
    return {"name": settings.app_name, "version": settings.version, "docs": "/docs"}
