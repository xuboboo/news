"""公众号排版草稿 API：每日早报 → 一键排版的草稿 CRUD。

与 drafts.py（选题生文）同款共享口径：require_user 即可读写，created_by 仅作
追溯。排版为确定性渲染（不经 LLM、不入队列、同步完成），因此不受
article_gen_enabled / 每小时生成预算约束——零 token 成本的操作无需计入成本防线。
"""
from datetime import date as date_type, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import current_user_of, require_json_mutation
from app.core.db import get_db
from app.models.report import DailyReport
from app.models.wx_layout_draft import WxLayoutDraft
from app.services import audit_service
from app.services.wx_layout_service import build_layout

router = APIRouter(prefix="/api/v2", dependencies=[Depends(require_json_mutation)])


def _draft_dict(r: WxLayoutDraft, with_content: bool = False) -> dict:
    out = {
        "id": r.id,
        "report_id": r.report_id,
        "report_date": r.report_date.isoformat() if r.report_date else "",
        "issue_no": r.issue_no,
        "title": r.title or "",
        "digest": r.digest or "",
        "gen_count": r.gen_count or 1,
        "revision": r.revision or 1,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
    if with_content:
        out["content_html"] = r.content_html or ""
    return out


def _parse_date(raw: object) -> date_type:
    try:
        return datetime.strptime(str(raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "date 格式须为 YYYY-MM-DD")


def _get_report_or_404(db: Session, day: date_type) -> DailyReport:
    row = db.query(DailyReport).filter(DailyReport.date == day).first()
    if row is None:
        raise HTTPException(404, "该日期暂无早报，请先在「运维监控」生成早报")
    return row


def _render(report: DailyReport) -> dict:
    """早报快照 → 排版产物；内容为空（无事件/速览/热榜）时拒绝产稿。"""
    try:
        return build_layout(report.content or {})
    except ValueError as exc:
        raise HTTPException(400, f"该期早报{exc}，无法排版")


def _get_draft_or_404(db: Session, draft_id: int) -> WxLayoutDraft:
    row = db.get(WxLayoutDraft, draft_id)
    if row is None:
        raise HTTPException(404, "draft not found")
    return row


def _require_revision(body: dict, row: WxLayoutDraft) -> None:
    expected = body.get("revision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if expected != (row.revision or 1):
        raise HTTPException(409, "草稿已被他人修改，请刷新后重试")


@router.post("/wx-layouts")
def create_or_regenerate_wx_layout(
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """按日期排版早报：{date: "YYYY-MM-DD"}。

    同一期早报只保留一份排版草稿（uq_wx_layout_report）：已存在则覆盖更新
    （gen_count +1）并返回 regenerated=true——早报可在「运维监控」重建，
    重复点击「生成」即以最新早报内容重新排版，对使用者是幂等语义。
    """
    body = payload or {}
    day = _parse_date(body.get("date"))
    report = _get_report_or_404(db, day)
    rendered = _render(report)

    user = current_user_of(request)
    existing = db.query(WxLayoutDraft).filter(WxLayoutDraft.report_id == report.id).first()
    if existing is None:
        row = WxLayoutDraft(
            report_id=report.id,
            report_date=report.date,
            issue_no=report.issue_no or 1,
            created_by=user.id if user else None,
            **rendered,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # 并发双击：唯一约束兜底，转为对既有行的覆盖更新
            db.rollback()
            existing = db.query(WxLayoutDraft).filter(WxLayoutDraft.report_id == report.id).first()
            if existing is None:
                raise HTTPException(409, "排版草稿创建冲突，请刷新后重试")
            return _update_existing(db, existing, report, rendered, request, user)
        db.commit()
        db.refresh(row)
        audit_service.record(
            db, "wx_layout.generated", request=request, user=user,
            detail={"draft_id": row.id, "report_id": report.id, "report_date": report.date.isoformat()},
        )
        return {"regenerated": False, **_draft_dict(row)}
    return _update_existing(db, existing, report, rendered, request, user)


def _update_existing(
    db: Session, row: WxLayoutDraft, report: DailyReport, rendered: dict,
    request: Request, user,
) -> dict:
    # Increment counters in SQL so concurrent regenerations cannot overwrite each
    # other's generation/revision increments. The newest rendered content wins.
    updated = db.query(WxLayoutDraft).filter(WxLayoutDraft.id == row.id).update(
        {
            WxLayoutDraft.title: rendered["title"],
            WxLayoutDraft.digest: rendered["digest"],
            WxLayoutDraft.content_html: rendered["content_html"],
            WxLayoutDraft.issue_no: report.issue_no or row.issue_no or 1,
            WxLayoutDraft.gen_count: func.coalesce(WxLayoutDraft.gen_count, 1) + 1,
            WxLayoutDraft.revision: func.coalesce(WxLayoutDraft.revision, 1) + 1,
        },
        synchronize_session=False,
    )
    if not updated:
        db.rollback()
        raise HTTPException(409, "草稿已被删除，请刷新后重试")
    db.commit()
    db.refresh(row)
    audit_service.record(
        db, "wx_layout.regenerated", request=request, user=user,
        detail={"draft_id": row.id, "report_id": report.id, "gen_count": row.gen_count},
    )
    return {"regenerated": True, **_draft_dict(row, with_content=True)}


@router.get("/wx-layouts")
def list_wx_layouts(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """排版草稿列表（新→旧，不含正文 HTML）。"""
    query = db.query(WxLayoutDraft)
    total = query.count()
    rows = query.order_by(WxLayoutDraft.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_draft_dict(r) for r in rows], "total": total}


@router.get("/wx-layouts/{draft_id}")
def get_wx_layout(draft_id: int, db: Session = Depends(get_db)):
    """排版草稿详情（含整篇 HTML，预览与复制共用）。"""
    return _draft_dict(_get_draft_or_404(db, draft_id), with_content=True)


@router.post("/wx-layouts/{draft_id}/regenerate")
def regenerate_wx_layout(
    draft_id: int,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """以源早报当前内容重新排版（早报重建后同步排版稿）。"""
    row = _get_draft_or_404(db, draft_id)
    report = db.get(DailyReport, row.report_id)
    if report is None:
        raise HTTPException(404, "源早报已不存在，无法重新生成")
    rendered = _render(report)
    return _update_existing(db, row, report, rendered, request, current_user_of(request))


@router.delete("/wx-layouts/{draft_id}")
def delete_wx_layout(
    draft_id: int,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    row = _get_draft_or_404(db, draft_id)
    _require_revision(payload or {}, row)
    detail = {"draft_id": row.id, "report_date": row.report_date.isoformat() if row.report_date else ""}
    deleted = db.query(WxLayoutDraft).filter(
        WxLayoutDraft.id == draft_id,
        WxLayoutDraft.revision == (row.revision or 1),
    ).delete(synchronize_session=False)
    if not deleted:
        db.rollback()
        raise HTTPException(409, "草稿已被他人修改，请刷新后重试")
    db.commit()
    audit_service.record(
        db, "wx_layout.deleted", request=request,
        user=current_user_of(request), detail=detail,
    )
    return {"deleted": True}
