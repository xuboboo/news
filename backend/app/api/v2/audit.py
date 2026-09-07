"""审计日志查询 API：/api/v2/admin/audit-logs（仅平台超管）。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import require_super_admin
from app.core.db import get_db
from app.services import audit_service

router = APIRouter(prefix="/api/v2/admin")


@router.get("/audit-logs", dependencies=[Depends(require_super_admin)])
def audit_logs(
    action: str | None = None,
    user_id: int | None = None,
    tenant_id: int | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return audit_service.query_logs(
        db, page=page, page_size=page_size,
        action=action, user_id=user_id, tenant_id=tenant_id, q=q,
    )


@router.get("/audit-actions", dependencies=[Depends(require_super_admin)])
def audit_actions():
    """审计动作清单（前端筛选下拉）。"""
    return {"items": sorted(audit_service.ACTIONS)}
