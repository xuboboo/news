"""网站资讯文章草稿 API。

与 /drafts 完全分开：使用同一张表但 channel=site，列表、详情、编辑、重生成、
删除和审计动作均独立；旧公众号草稿不会出现在这里。
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
    SITE_COVER_GUIDANCE_LIMIT,
    SITE_DIGEST_LIMIT,
    SITE_KEYWORDS_LIMIT,
    SITE_TAGS_LIMIT,
    SITE_TITLE_LIMIT,
    source_refs_from_content,
    source_refs_from_material,
)
from app.utils.text import truncate

router = APIRouter(prefix="/api/v2", dependencies=[Depends(require_json_mutation)])
_SOURCE_TYPES = ("event", "article")
_TIERS = ("brief", "depth")
_STATUSES = ("generating", "ready", "failed")
GENERATING_STALE_MINUTES = 15


def _site_dict(row: ArticleDraft, with_content: bool = False) -> dict:
    result = {
        "id": row.id,
        "channel": "site",
        "status": row.status,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "source_title": row.source_title,
        "tier": row.tier,
        "title": row.title or "",
        "alt_titles": row.alt_titles or [],
        "digest": row.digest or "",
        "seo_keywords": row.seo_keywords or "",
        "site_tags": row.site_tags or "",
        "cover_guidance": row.cover_guidance or "",
        "software_id": row.software_id or 0,
        "is_pinned": bool(row.is_pinned),
        "is_recommended": bool(row.is_recommended),
        "is_headline": bool(row.is_headline),
        "publish_at": row.publish_at.isoformat() if row.publish_at else None,
        "model": row.model or "",
        "prompt_tokens": row.prompt_tokens or 0,
        "completion_tokens": row.completion_tokens or 0,
        "duration_ms": row.duration_ms or 0,
        "error": row.error or "",
        "gen_count": row.gen_count or 1,
        "revision": row.revision or 1,
        "source_refs": row.source_refs or (source_refs_from_content(row.content_md) if with_content else []),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if with_content:
        result["content_md"] = row.content_md or ""
    return result


def _require_enabled(db: Session) -> None:
    if not config_service.get_effective(db, "article_gen_enabled"):
        raise HTTPException(403, "文章生成已在运维面板关闭（article_gen_enabled=false）")


def _check_budget(db: Session, user) -> None:
    if user is None:
        return
    limit = int(config_service.get_effective(db, "article_gen_hourly_limit") or 0)
    if limit <= 0:
        return
    if db.bind is None or db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('article_gen_limit:' || :uid))"), {"uid": user.id})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    used = db.query(AuditLog.id).filter(
        AuditLog.action.in_(("draft.generated", "draft.regenerated", "site_draft.generated", "site_draft.regenerated")),
        AuditLog.user_id == user.id, AuditLog.created_at >= cutoff,
    ).count()
    if used >= limit:
        raise HTTPException(429, f"已达每小时生成上限（{limit} 次），请稍后再试")


def _dispatch(db: Session, row: ArticleDraft, actor_id: int | None = None) -> None:
    from app.tasks.tasks import site_article_gen_task

    draft_id, generation = row.id, row.gen_count
    try:
        site_article_gen_task.delay(draft_id, trigger="manual", generation=generation)
    except Exception as exc:
        db.rollback()
        db.query(ArticleDraft).filter(
            ArticleDraft.id == draft_id, ArticleDraft.channel == "site",
            ArticleDraft.gen_count == generation, ArticleDraft.status == "generating",
        ).update({ArticleDraft.status: "failed", ArticleDraft.error: truncate(f"任务入队失败：{exc}", 500),
                  ArticleDraft.revision: ArticleDraft.revision + 1}, synchronize_session=False)
        db.commit()
        audit_service.record(db, "site_draft.dispatch_failed", user_id=actor_id,
                             detail={"draft_id": draft_id, "generation": generation})
    finally:
        db.expire_all()
        refreshed = db.get(ArticleDraft, draft_id, populate_existing=True)
        if refreshed is None or refreshed.channel != "site":
            raise HTTPException(404, "网站草稿已被删除")
        row.status, row.gen_count, row.error = refreshed.status, refreshed.gen_count, refreshed.error


def _get(db: Session, draft_id: int) -> ArticleDraft:
    row = db.get(ArticleDraft, draft_id)
    if row is None or row.channel != "site":
        raise HTTPException(404, "网站文章草稿不存在")
    return row


def _source(db: Session, source_type: str, source_id: int):
    return db.get(HotEvent if source_type == "event" else Article, source_id)


def _parse_publish_at(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "发布时间必须是 ISO 格式字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, "发布时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed


def _validate_text(body: dict, field: str, limit: int, label: str, patch: dict) -> None:
    if field not in body:
        return
    value = body[field]
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HTTPException(400, f"{label}必须是字符串")
    value = value.strip()
    if len(value) > limit:
        raise HTTPException(400, f"{label}去除首尾空白后最多{limit}个字符")
    patch[field] = value


@router.post("/site-drafts")
def create_site_draft(request: Request, payload: dict | None = None, db: Session = Depends(get_db)):
    """创建网站资讯草稿并入队。网站字段由生成器起草，图片、软件关联和特殊属性由编辑填写。"""
    _require_enabled(db)
    body = payload or {}
    if not isinstance(body, dict):
        raise HTTPException(400, "请求正文必须是 JSON 对象")
    if str(body.get("channel") or "site").strip() != "site":
        raise HTTPException(400, "公众号草稿请使用公众号入口")
    source_type = str(body.get("source_type") or "").strip()
    tier = str(body.get("tier") or "brief").strip()
    source_id = body.get("source_id")
    if not isinstance(source_id, int) or isinstance(source_id, bool) or not 0 < source_id <= 2147483647:
        raise HTTPException(400, "source_id must be a positive integer")
    if source_type not in _SOURCE_TYPES:
        raise HTTPException(400, "source_type 仅支持 event/article")
    if tier not in _TIERS:
        raise HTTPException(400, "tier 仅支持 brief/depth")
    src = _source(db, source_type, source_id)
    if src is None:
        raise HTTPException(404, "source not found")
    user = current_user_of(request)
    _check_budget(db, user)
    row = ArticleDraft(channel="site", source_type=source_type, source_id=source_id,
                       source_title=(src.title or "")[:500], tier=tier,
                       created_by=user.id if user else None)
    db.add(row)
    db.flush()
    audit_service.record(db, "site_draft.generated", request=request, user=user,
                         detail={"draft_id": row.id, "source_type": source_type,
                                 "source_id": source_id, "tier": tier,
                                 "status": "generating", "generation": row.gen_count})
    db.refresh(row)
    _dispatch(db, row, user.id if user else None)
    return _site_dict(row)


@router.get("/site-drafts")
def list_site_drafts(status: str | None = Query(None), limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0), before_id: int | None = Query(None, ge=1),
                     db: Session = Depends(get_db)):
    query = db.query(ArticleDraft).filter(ArticleDraft.channel == "site")
    if status:
        if status not in _STATUSES:
            raise HTTPException(400, "status 仅支持 generating/ready/failed")
        query = query.filter(ArticleDraft.status == status)
    total = query.count()
    if before_id is not None:
        query = query.filter(ArticleDraft.id < before_id)
    rows = query.order_by(ArticleDraft.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_site_dict(row) for row in rows], "total": total}


@router.get("/site-drafts/{draft_id}")
def get_site_draft(draft_id: int, db: Session = Depends(get_db)):
    return _site_dict(_get(db, draft_id), with_content=True)


@router.put("/site-drafts/{draft_id}")
def update_site_draft(draft_id: int, request: Request, payload: dict | None = None,
                      db: Session = Depends(get_db)):
    row = _get(db, draft_id)
    if row.status == "generating":
        raise HTTPException(409, "生成中，请等生成结束后再编辑")
    body = payload or {}
    revision = body.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if revision != (row.revision or 1):
        raise HTTPException(409, "网站草稿已被他人修改，请刷新后重试")

    patch: dict = {}
    _validate_text(body, "title", SITE_TITLE_LIMIT, "标题", patch)
    _validate_text(body, "digest", SITE_DIGEST_LIMIT, "内容简介", patch)
    _validate_text(body, "seo_keywords", SITE_KEYWORDS_LIMIT, "页面关键字", patch)
    _validate_text(body, "site_tags", SITE_TAGS_LIMIT, "标签", patch)
    _validate_text(body, "cover_guidance", SITE_COVER_GUIDANCE_LIMIT, "标题图片建议", patch)
    if "title" in patch and not patch["title"]:
        raise HTTPException(400, "标题不能为空")
    if "digest" in patch and not patch["digest"]:
        raise HTTPException(400, "内容简介不能为空")
    if "software_id" in body:
        value = body["software_id"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2147483647:
            raise HTTPException(400, "关联软件 ID 必须是非负整数")
        patch["software_id"] = value
    for field in ("is_pinned", "is_recommended", "is_headline"):
        if field in body:
            if not isinstance(body[field], bool):
                raise HTTPException(400, f"{field}必须是布尔值")
            patch[field] = body[field]
    if "publish_at" in body:
        patch["publish_at"] = _parse_publish_at(body["publish_at"])
    if "content_md" in body:
        value = body["content_md"]
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise HTTPException(400, "新闻正文必须是字符串")
        if not value.strip() and row.status == "ready":
            raise HTTPException(400, "新闻正文不能为空")
        if value.strip():
            source_refs = row.source_refs or source_refs_from_content(row.content_md)
            if not source_refs:
                from app.services.article_gen_service import _collect_material
                material, _ = _collect_material(db, row)
                source_refs = source_refs_from_material(material)
            from app.services.article_gen_service import _ensure_compliance_footer
            value = _ensure_compliance_footer(value, source_refs)
            patch["source_refs"] = source_refs
        patch["content_md"] = value

    updates = {getattr(ArticleDraft, key): value for key, value in patch.items()}
    if updates:
        updates[ArticleDraft.revision] = ArticleDraft.revision + 1
        changed = db.query(ArticleDraft).filter(
            ArticleDraft.id == draft_id, ArticleDraft.channel == "site",
            ArticleDraft.revision == revision, ArticleDraft.status != "generating",
        ).update(updates, synchronize_session=False)
        if not changed:
            db.rollback()
            raise HTTPException(409, "草稿状态已变化，请刷新后重试")
    db.commit()
    db.refresh(row)
    audit_service.record(db, "site_draft.updated", request=request, user=current_user_of(request),
                         detail={"draft_id": row.id, "fields": sorted(patch)})
    return _site_dict(row, with_content=True)


@router.post("/site-drafts/{draft_id}/regenerate")
def regenerate_site_draft(draft_id: int, request: Request, payload: dict | None = None,
                          db: Session = Depends(get_db)):
    _require_enabled(db)
    row = _get(db, draft_id)
    body = payload or {}
    revision = body.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if revision != (row.revision or 1):
        raise HTTPException(409, "网站草稿已被他人修改，请刷新后重试")
    if row.status == "generating":
        updated = row.updated_at or row.created_at
        age = 0.0
        if updated:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated).total_seconds() / 60
        if age < GENERATING_STALE_MINUTES:
            raise HTTPException(409, "已在生成中，请勿重复触发")
    user = current_user_of(request)
    _check_budget(db, user)
    expected_count, expected_status, expected_updated = row.gen_count or 1, row.status, row.updated_at
    reserve = db.query(ArticleDraft).filter(
        ArticleDraft.id == row.id, ArticleDraft.channel == "site",
        ArticleDraft.gen_count == expected_count, ArticleDraft.revision == revision,
        ArticleDraft.status == expected_status,
    )
    reserve = reserve.filter(ArticleDraft.updated_at == expected_updated if expected_updated is not None else ArticleDraft.updated_at.is_(None))
    changed = reserve.update({ArticleDraft.status: "generating", ArticleDraft.error: "",
                              ArticleDraft.gen_count: expected_count + 1,
                              ArticleDraft.revision: ArticleDraft.revision + 1}, synchronize_session=False)
    if not changed:
        db.rollback()
        raise HTTPException(409, "草稿状态已变化，请刷新后重试")
    db.refresh(row)
    audit_service.record(db, "site_draft.regenerated", request=request, user=user,
                         detail={"draft_id": row.id, "generation": row.gen_count, "status": "generating"})
    _dispatch(db, row, user.id if user else None)
    return _site_dict(row)


@router.delete("/site-drafts/{draft_id}")
def delete_site_draft(draft_id: int, request: Request, payload: dict | None = None,
                      db: Session = Depends(get_db)):
    row = _get(db, draft_id)
    body = payload or {}
    revision = body.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise HTTPException(428, "缺少有效草稿版本，请刷新后重试")
    if revision != (row.revision or 1):
        raise HTTPException(409, "网站草稿已被他人修改，请刷新后重试")
    deleted = db.query(ArticleDraft).filter(
        ArticleDraft.id == draft_id, ArticleDraft.channel == "site", ArticleDraft.revision == revision,
    ).delete(synchronize_session=False)
    if not deleted:
        db.rollback()
        raise HTTPException(409, "草稿已被他人修改，请刷新后重试")
    detail = {"draft_id": row.id, "title": row.title or row.source_title}
    db.commit()
    audit_service.record(db, "site_draft.deleted", request=request, user=current_user_of(request), detail=detail)
    return {"deleted": True}
