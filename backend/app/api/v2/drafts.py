"""文章草稿 API：「选题 → AI 生成 → 编辑 → 复制发布」链路的草稿 CRUD。

全员共享（与资讯数据同口径，租户只隔离用户与权限）：require_user 即可读写，
created_by 仅作追溯。生成/重新生成受热配置 article_gen_enabled 总开关控制，
并按 article_gen_hourly_limit 限流——这是全站第一个由成员触发的 LLM 花费点，
放任无限派发等于把成本看板的预算告警当唯一防线。
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.deps import current_user_of, require_json_mutation
from app.core.db import get_db
from app.models.article import Article
from app.models.article_draft import ArticleDraft
from app.models.audit_log import AuditLog
from app.models.event import HotEvent
from app.services import audit_service, config_service
from app.services.article_draft_rules import (
    DIGEST_LIMIT,
    TITLE_LIMIT,
    source_refs_from_content,
    source_refs_from_material,
)
from app.utils.text import truncate

router = APIRouter(prefix="/api/v2", dependencies=[Depends(require_json_mutation)])

_SOURCE_TYPES = ("event", "article")
_TIERS = ("brief", "depth")
_DRAFT_STATUSES = ("generating", "ready", "failed")

# generating 超过此时长视为卡死（worker 崩溃/入队丢失），放行重新生成；
# 正常生成上限 180s（CHAT_TIMEOUT_SECONDS）+ 队列排队，15 分钟远宽于正常耗时
GENERATING_STALE_MINUTES = 15


def _draft_dict(r: ArticleDraft, with_content: bool = False) -> dict:
    """草稿序列化；列表不带 content_md（长文拖慢列表响应），详情才带。"""
    out = {
        "id": r.id,
        "channel": r.channel or "wechat",
        "status": r.status,
        "source_type": r.source_type,
        "source_id": r.source_id,
        "source_title": r.source_title,
        "tier": r.tier,
        "title": r.title or "",
        "alt_titles": r.alt_titles or [],
        "longtail_suggestions": getattr(r, "longtail_suggestions", None) or [],
        "digest": r.digest or "",
        "model": r.model or "",
        "prompt_tokens": r.prompt_tokens or 0,
        "completion_tokens": r.completion_tokens or 0,
        "duration_ms": r.duration_ms or 0,
        "error": r.error or "",
        "gen_count": r.gen_count or 1,
        "revision": r.revision or 1,
        "source_refs": r.source_refs or (source_refs_from_content(r.content_md) if with_content else []),
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
    if with_content:
        from app.services.article_gen_service import _strip_generated_footer_sections
        out["content_md"] = _strip_generated_footer_sections(r.content_md or "", strip_sources=False)
    return out


def _require_enabled(db: Session) -> None:
    if not config_service.get_effective(db, "article_gen_enabled"):
        raise HTTPException(403, "文章生成已在运维面板关闭（article_gen_enabled=false）")


def _check_hourly_budget(db: Session, user) -> None:
    """单用户每小时生成派发上限。发布结果不确定时保守计入额度，避免重复付费派发。"""
    if user is None:
        return
    limit = int(config_service.get_effective(db, "article_gen_hourly_limit") or 0)
    if limit <= 0:
        return
    # 先按用户串行化「计数 → 本次派发审计预留」事务；仅 count 后再提交会有
    # 并发穿透，多个同时请求都可能读到同一个 used，从而超出成本上限。
    if db.bind is None or db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('article_gen_limit:' || :uid))"),
            {"uid": user.id},
        )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = (
        db.query(AuditLog.action, AuditLog.detail)
        .filter(
            AuditLog.action.in_(("draft.generated", "draft.regenerated", "site_draft.generated", "site_draft.regenerated")),
            AuditLog.user_id == user.id,
            AuditLog.created_at >= cutoff,
        )
        .all()
    )
    used = len(rows)
    if int(used or 0) >= limit:
        raise HTTPException(
            429,
            f"已达每小时生成上限（{limit} 次），请稍后再试；可在运维面板调整 article_gen_hourly_limit",
        )


def _dispatch(db: Session, row: ArticleDraft, actor_id: int | None = None) -> None:
    """派发固定代次；发布异常的接受状态可能不确定，失败审计不返还额度。"""
    from app.tasks.tasks import article_gen_task

    draft_id, expected_generation = row.id, row.gen_count
    try:
        article_gen_task.delay(draft_id, trigger="manual", generation=expected_generation)
    except Exception as exc:
        db.rollback()
        db.query(ArticleDraft).filter(
            ArticleDraft.id == draft_id,
            ArticleDraft.channel == "wechat",
            ArticleDraft.gen_count == expected_generation,
            ArticleDraft.status == "generating",
        ).update({
            ArticleDraft.status: "failed",
            ArticleDraft.error: truncate(f"任务入队失败：{exc}", 500),
            ArticleDraft.revision: ArticleDraft.revision + 1,
        }, synchronize_session=False)
        db.commit()
        audit_service.record(
            db, "draft.dispatch_failed", user_id=actor_id,
            detail={"draft_id": draft_id, "generation": expected_generation},
        )
    finally:
        # 回滚可能使 ORM 实例过期；刷新只读取，不会重建已删除记录。
        db.expire_all()
        refreshed = db.get(ArticleDraft, draft_id, populate_existing=True)
        if refreshed is None or refreshed.channel != "wechat":
            raise HTTPException(404, "草稿已被删除")
        row.status = refreshed.status
        row.gen_count = refreshed.gen_count
        row.error = refreshed.error


def _get_draft_or_404(db: Session, draft_id: int) -> ArticleDraft:
    row = db.get(ArticleDraft, draft_id)
    if row is None or row.channel != "wechat":
        raise HTTPException(404, "draft not found")
    return row


@router.post("/drafts")
def create_draft(
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """按选题建草稿并入队生成：{source_type: event|article, source_id, tier: brief|depth}。"""
    _require_enabled(db)
    body = payload or {}
    if str(body.get("channel") or "wechat").strip() != "wechat":
        raise HTTPException(400, "网站资讯请使用网站文章草稿入口")
    source_type = str(body.get("source_type") or "").strip()
    tier = str(body.get("tier") or "brief").strip()
    raw_source_id = body.get("source_id")
    source_id = raw_source_id if isinstance(raw_source_id, int) and not isinstance(raw_source_id, bool) else 0
    if not isinstance(body.get("source_id"), int) or isinstance(body.get("source_id"), bool):
        raise HTTPException(400, "source_id must be an integer")
    if source_type not in _SOURCE_TYPES:
        raise HTTPException(400, "source_type 仅支持 event/article")
    if tier not in _TIERS:
        raise HTTPException(400, "tier 仅支持 brief（快讯版）/depth（深度版）")
    if source_id <= 0:
        raise HTTPException(400, "source_id must be positive")

    if source_type == "event":
        src = db.get(HotEvent, source_id)
    else:
        src = db.get(Article, source_id)
    if src is None:
        raise HTTPException(404, "source not found")

    user = current_user_of(request)
    _check_hourly_budget(db, user)
    row = ArticleDraft(
        source_type=source_type,
        source_id=source_id,
        source_title=(src.title or "")[:500],
        tier=tier,
        created_by=user.id if user else None,
    )
    db.add(row)
    db.flush()
    # 与用户级限流锁处于同一事务：审计写入提交时一并持久化草稿并释放锁，
    # 使下一个并发请求能看到本次额度占用。
    audit_service.record(
        db, "draft.generated", request=request, user=user,
        detail={"draft_id": row.id, "source_type": source_type, "source_id": source_id,
                "tier": tier, "status": "generating", "generation": row.gen_count},
    )
    db.refresh(row)

    # 入队放在草稿落库之后：队列消费方以草稿行为唯一事实源；
    # 入队失败在 _dispatch 内落 failed，不会卡 generating
    _dispatch(db, row, user.id if user else None)
    return _draft_dict(row)


@router.get("/drafts")
def list_drafts(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    before_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    """草稿列表（新→旧，不含正文）；status 可选过滤。"""
    query = db.query(ArticleDraft).filter(ArticleDraft.channel == "wechat")
    if status:
        if status not in _DRAFT_STATUSES:
            raise HTTPException(400, "status 仅支持 generating/ready/failed")
        query = query.filter(ArticleDraft.status == status)
    total = query.count()
    if before_id is not None:
        query = query.filter(ArticleDraft.id < before_id)
    rows = query.order_by(ArticleDraft.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_draft_dict(r) for r in rows], "total": total}


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: int, db: Session = Depends(get_db)):
    """草稿详情（含正文）：前端生成中轮询与编辑页共用。"""
    return _draft_dict(_get_draft_or_404(db, draft_id), with_content=True)


@router.put("/drafts/{draft_id}")
def update_draft(
    draft_id: int,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """手动编辑保存：{title?, digest?, content_md?}，仅更新出现的字段。"""
    row = _get_draft_or_404(db, draft_id)
    if row.status == "generating":
        raise HTTPException(409, "生成中，请等生成结束后再编辑")
    body = payload or {}
    expected_revision = body.get("revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if expected_revision != (row.revision or 1):
        raise HTTPException(409, "草稿已被他人修改，请刷新后重新应用你的编辑")
    # 先校验完整 patch 再改 ORM，避免合法字段先写入、后续字段失败时产生部分更新。
    patch = {}
    for field, label, limit in (("title", "标题", TITLE_LIMIT), ("digest", "摘要", DIGEST_LIMIT)):
        if field not in body:
            continue
        value = body[field]
        # 兼容既有 API：null 与空字符串均表示清空；其他非字符串明确拒绝。
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise HTTPException(400, f"{label}必须是字符串")
        value = value.strip()
        if len(value) > limit:
            raise HTTPException(400, f"{label}去除首尾空白后最多{limit}个字符")
        patch[field] = value
    if "content_md" in body:
        value = body["content_md"]
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise HTTPException(400, "正文必须是字符串")
        if not value.strip():
            if row.status == "ready":
                raise HTTPException(400, "正文不能为空")
        else:
            source_refs = row.source_refs or source_refs_from_content(row.content_md)
            if not source_refs:
                from app.services.article_gen_service import _collect_material
                material, _ = _collect_material(db, row)
                source_refs = source_refs_from_material(material)
            from app.services.article_gen_service import _ensure_source_footer
            value = _ensure_source_footer(value, source_refs)
            patch["source_refs"] = source_refs
        patch["content_md"] = value

    updates = {getattr(ArticleDraft, field): value for field, value in patch.items()}
    if updates:
        updates[ArticleDraft.revision] = ArticleDraft.revision + 1
        stmt = db.query(ArticleDraft).filter(
            ArticleDraft.id == draft_id,
            ArticleDraft.channel == "wechat",
            ArticleDraft.revision == expected_revision,
            ArticleDraft.status != "generating",
        )
        stmt = stmt.filter(ArticleDraft.status != "generating")
        if not stmt.update(updates, synchronize_session=False):
            db.rollback()
            raise HTTPException(409, "草稿状态已变化，请刷新后重试")
    db.commit()
    db.refresh(row)
    audit_service.record(
        db, "draft.updated", request=request,
        user=current_user_of(request),
        detail={"draft_id": row.id, "fields": sorted(k for k in body if k in ("title", "digest", "content_md"))},
    )
    return _draft_dict(row, with_content=True)


@router.post("/drafts/{draft_id}/regenerate")
def regenerate_draft(
    draft_id: int,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    """重新生成：正文/标题/摘要将被覆盖（前端已二次确认），gen_count +1。

    generating 状态默认 409 防重复派发；但超过 GENERATING_STALE_MINUTES 仍
    停在 generating 的草稿视为卡死（worker 崩溃/入队丢失），放行重试自救。
    """
    _require_enabled(db)
    row = _get_draft_or_404(db, draft_id)
    body = payload or {}
    expected_revision = body.get("revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if expected_revision != (row.revision or 1):
        raise HTTPException(409, "草稿已被他人修改，请刷新后重试")
    user = current_user_of(request)
    if row.status == "generating":
        updated = row.updated_at or row.created_at
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(timezone.utc) - updated).total_seconds() / 60
        else:
            age_minutes = 0.0
        if age_minutes < GENERATING_STALE_MINUTES:
            raise HTTPException(409, "已在生成中，请勿重复触发")
    expected_count = row.gen_count or 1
    expected_status = row.status
    expected_updated = row.updated_at
    _check_hourly_budget(db, user)
    reserve = db.query(ArticleDraft).filter(
        ArticleDraft.id == row.id,
        ArticleDraft.channel == "wechat",
        ArticleDraft.gen_count == expected_count,
        ArticleDraft.revision == expected_revision,
        ArticleDraft.status == expected_status,
    )
    reserve = reserve.filter(
        ArticleDraft.updated_at == expected_updated if expected_updated is not None
        else ArticleDraft.updated_at.is_(None)
    )
    changed = reserve.update({
        ArticleDraft.status: "generating",
        ArticleDraft.error: "",
        ArticleDraft.gen_count: expected_count + 1,
        ArticleDraft.revision: ArticleDraft.revision + 1,
    }, synchronize_session=False)
    if not changed:
        db.rollback()
        raise HTTPException(409, "草稿状态已变化，请刷新后重试")
    db.refresh(row)
    audit_service.record(
        db, "draft.regenerated", request=request, user=user,
        detail={"draft_id": row.id, "gen_count": row.gen_count, "status": "generating",
                "generation": row.gen_count},
    )

    _dispatch(db, row, user.id if user else None)
    return _draft_dict(row)


@router.delete("/drafts/{draft_id}")
def delete_draft(
    draft_id: int,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
):
    row = _get_draft_or_404(db, draft_id)
    body = payload or {}
    expected_revision = body.get("revision")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if expected_revision != (row.revision or 1):
        raise HTTPException(409, "草稿已被他人修改，请刷新后重试")
    detail = {"draft_id": row.id, "title": row.title or row.source_title}
    deleted = db.query(ArticleDraft).filter(
        ArticleDraft.id == draft_id,
        ArticleDraft.channel == "wechat",
        ArticleDraft.revision == expected_revision,
    ).delete(synchronize_session=False)
    if not deleted:
        db.rollback()
        raise HTTPException(409, "草稿已被他人修改，请刷新后重试")
    db.commit()
    audit_service.record(
        db, "draft.deleted", request=request,
        user=current_user_of(request), detail=detail,
    )
    return {"deleted": True}
