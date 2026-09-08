"""审计日志查询/导出 API：/api/v2/admin/audit-logs*（仅平台超管）。"""
import csv
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_super_admin
from app.core.db import get_db
from app.services import audit_service

router = APIRouter(prefix="/api/v2/admin")


def _parse_time_or_400(value: str | None) -> datetime | None:
    try:
        return audit_service.parse_time_param(value)
    except ValueError as exc:
        raise HTTPException(400, f"时间格式不正确（需 ISO 8601，如 2026-09-01T00:00:00Z）：{exc}")


@router.get("/audit-logs", dependencies=[Depends(require_super_admin)])
def audit_logs(
    action: str | None = None,
    user_id: int | None = None,
    tenant_id: int | None = None,
    q: str | None = None,
    ip: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return audit_service.query_logs(
        db, page=page, page_size=page_size,
        action=action, user_id=user_id, tenant_id=tenant_id, q=q,
        ip=ip, time_from=_parse_time_or_400(time_from), time_to=_parse_time_or_400(time_to),
    )


@router.get("/audit-logs/export", dependencies=[Depends(require_super_admin)])
def audit_logs_export(
    action: str | None = None,
    user_id: int | None = None,
    tenant_id: int | None = None,
    q: str | None = None,
    ip: str | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    db: Session = Depends(get_db),
):
    """CSV 导出（与列表同筛选；id 升序，上限见 EXPORT_ROW_CAP）。应急响应/留档用。"""
    # 时间解析必须在端点体内完成：生成器在流式阶段才执行，彼时再抛 400 已无法改变响应状态码
    t_from = _parse_time_or_400(time_from)
    t_to = _parse_time_or_400(time_to)

    def _rows():
        # BOM：让 Excel 按 UTF-8 识别中文
        yield "\ufeff"
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "time_utc", "action", "user_id", "user_email",
             "tenant_id", "ip", "user_agent", "detail_json"]
        )
        yield buf.getvalue()
        truncated = False
        for r in audit_service.iter_logs(
            db, action=action, user_id=user_id, tenant_id=tenant_id, q=q,
            ip=ip, time_from=t_from, time_to=t_to,
        ):
            if r is audit_service.EXPORT_ROW_CAP_REACHED:
                truncated = True
                break
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [r.id, r.created_at.isoformat() if r.created_at else "",
                 r.action, r.user_id or "", r.user_email, r.tenant_id or "",
                 r.ip, r.user_agent, json.dumps(r.detail or {}, ensure_ascii=False, sort_keys=True)]
            )
            yield buf.getvalue()
        if truncated:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([f"（已达单次导出上限 {audit_service.EXPORT_ROW_CAP} 行，请用时间范围分段导出）"])
            yield buf.getvalue()

    filename = f"audit-logs-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return StreamingResponse(
        _rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit-actions", dependencies=[Depends(require_super_admin)])
def audit_actions():
    """审计动作清单（前端筛选下拉）。"""
    return {"items": sorted(audit_service.ACTIONS)}
