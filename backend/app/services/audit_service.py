"""审计服务：统一埋点入口 + 查询 + 清理。

覆盖面（action 清单）：登录成功/失败/锁定/登出/改密/会话吊销、
租户与用户增改停、密码重置、运行时配置修改、LLM/Embedding 连通测试、
手动触发采集/聚合/日报。
"""
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User

# 动作清单：审计覆盖面的唯一权威定义（audit_auth.py 据此抽查）
ACTIONS = {
    # —— 认证与会话 ——
    "auth.login",
    "auth.login_failed",
    "auth.locked",
    "auth.logout",
    "auth.password_changed",
    "auth.session_revoked",
    # —— 租户与用户管理 ——
    "tenant.created",
    "tenant.updated",
    "user.created",
    "user.updated",
    "user.disabled",
    "user.password_reset",
    # —— 运维管理（管理面板敏感操作）——
    "admin.config_updated",
    "admin.test_llm",
    "admin.test_embedding",
    "admin.trigger_crawl",
    "admin.trigger_events",
    "admin.trigger_report",
}


def client_ip(request: Request | None) -> str:
    if request is None:
        return ""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def user_agent(request: Request | None) -> str:
    return (request.headers.get("user-agent", "") if request is not None else "")[:255]


def record(
    db: Session,
    action: str,
    *,
    request: Request | None = None,
    user: User | None = None,
    user_id: int | None = None,
    user_email: str = "",
    tenant_id: int | None = None,
    detail: dict | None = None,
    commit: bool = True,
) -> None:
    """写一条审计日志。传 user 对象或显式 user_id/user_email/tenant_id 之一均可。"""
    if user is not None:
        user_id = user.id
        user_email = user_email or user.email
        tenant_id = user.tenant_id
    db.add(
        AuditLog(
            action=action,
            user_id=user_id,
            user_email=user_email or "",
            tenant_id=tenant_id,
            detail=detail or {},
            ip=client_ip(request),
            user_agent=user_agent(request),
        )
    )
    if commit:
        db.commit()


def query_logs(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    action: str | None = None,
    user_id: int | None = None,
    tenant_id: int | None = None,
    q: str | None = None,
) -> dict:
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if tenant_id is not None:
        query = query.filter(AuditLog.tenant_id == tenant_id)
    if q:
        query = query.filter(AuditLog.user_email.ilike(f"%{q}%"))
    total = query.count()
    rows = (
        query.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "user_id": r.user_id,
                "user_email": r.user_email,
                "tenant_id": r.tenant_id,
                "action": r.action,
                "detail": r.detail or {},
                "ip": r.ip,
                "user_agent": r.user_agent,
            }
            for r in rows
        ],
        "total": total,
    }


def purge_older_than(db: Session, days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return (
        db.query(AuditLog).filter(AuditLog.created_at < cutoff).delete(synchronize_session=False)
    )
