"""租户与用户管理 API：/api/v2/admin/tenants*、/api/v2/admin/users*。

权限规则：
- super_admin：全局管理租户与用户，可分配任意角色；
- tenant_admin：仅本租户内管理 member（不能跨租户、不能建/改管理员与超管），
  越权目标一律 404（不暴露存在性）；
- 保护：平台至少保留一个 active 的 super_admin。
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import require_json_mutation, require_super_admin, require_tenant_admin
from app.auth import passwords
from app.core.db import get_db
from app.models.tenant import Tenant
from app.models.user import (
    ROLE_MEMBER,
    ROLE_SUPER_ADMIN,
    ROLES,
    ROLE_TENANT_ADMIN,
    User,
)
from app.services import audit_service

router = APIRouter(prefix="/api/v2/admin", dependencies=[Depends(require_json_mutation)])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email(email: str) -> str:
    email = _norm_email(email)
    if not email or len(email) > 255 or not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    return email


def _validate_password(password: str) -> str:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")
    return password


def _scope_filter(actor: User):
    """tenant_admin 只能看到本租户的 member；super 看全部。返回 (tenant_id|None, roles|None)。"""
    if actor.role == ROLE_TENANT_ADMIN:
        return actor.tenant_id, (ROLE_MEMBER,)
    return None, None


def _target_for(actor: User, db: Session, user_id: int) -> User:
    """按角色规则取可操作的目标用户；越权一律 404。"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    if actor.role == ROLE_TENANT_ADMIN:
        if target.tenant_id != actor.tenant_id or target.role != ROLE_MEMBER:
            raise HTTPException(404, "用户不存在")
    return target


def _guard_last_super(db: Session, target: User, *, demoting: bool, disabling: bool) -> None:
    if target.role != ROLE_SUPER_ADMIN or not (demoting or disabling):
        return
    active_supers = (
        db.query(func.count(User.id))
        .filter(User.role == ROLE_SUPER_ADMIN, User.status == "active", User.id != target.id)
        .scalar() or 0
    )
    if active_supers == 0:
        raise HTTPException(400, "平台至少保留一个启用的超管账号")


def _user_dict(db: Session, u: User, tenant_names: dict[int, str] | None = None) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "tenant_id": u.tenant_id,
        "tenant_name": (tenant_names or {}).get(u.tenant_id),
        "must_change_password": bool(u.must_change_password),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_by": u.created_by,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _tenant_names(db: Session) -> dict[int, str]:
    return {t.id: t.name for t in db.query(Tenant).all()}


# ———————————————————— 租户管理（super_admin）————————————————————

@router.get("/tenants", dependencies=[Depends(require_super_admin)])
def list_tenants(db: Session = Depends(get_db)):
    counts = dict(
        db.query(User.tenant_id, func.count(User.id)).group_by(User.tenant_id).all()
    )
    items = [
        {
            "id": t.id,
            "name": t.name,
            "status": t.status,
            "remark": t.remark,
            "user_count": counts.get(t.id, 0),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in db.query(Tenant).order_by(Tenant.id).all()
    ]
    return {"items": items, "total": len(items)}


@router.post("/tenants", dependencies=[Depends(require_super_admin)])
def create_tenant(
    payload: dict, request: Request,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    name = str((payload or {}).get("name") or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "租户名必填且长度 ≤ 64")
    if db.query(Tenant).filter_by(name=name).first():
        raise HTTPException(400, "租户名已存在")
    tenant = Tenant(
        name=name,
        remark=str((payload or {}).get("remark") or "")[:255],
    )
    db.add(tenant)
    db.commit()
    audit_service.record(db, "tenant.created", request=request, user=actor,
                         detail={"tenant_id": tenant.id, "name": name})
    return {"ok": True, "item": {"id": tenant.id, "name": tenant.name, "status": tenant.status}}


@router.put("/tenants/{tenant_id}", dependencies=[Depends(require_super_admin)])
def update_tenant(
    tenant_id: int, payload: dict, request: Request,
    actor: User = Depends(require_super_admin), db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "租户不存在")
    data = payload or {}
    detail: dict = {"tenant_id": tenant.id, "changes": []}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 64:
            raise HTTPException(400, "租户名必填且长度 ≤ 64")
        exists = db.query(Tenant).filter(Tenant.name == name, Tenant.id != tenant_id).first()
        if exists:
            raise HTTPException(400, "租户名已存在")
        if name != tenant.name:
            tenant.name = name
            detail["changes"].append("name")
    if "remark" in data:
        tenant.remark = str(data.get("remark") or "")[:255]
        detail["changes"].append("remark")
    if "status" in data:
        status = str(data.get("status") or "")
        if status not in ("active", "disabled"):
            raise HTTPException(400, "status 只能是 active / disabled")
        if status != tenant.status:
            tenant.status = status
            detail["changes"].append(f"status={status}")
    db.commit()
    audit_service.record(db, "tenant.updated", request=request, user=actor, detail=detail)
    return {"ok": True}


# ———————————————————— 用户管理 ————————————————————

@router.get("/users", dependencies=[Depends(require_tenant_admin)])
def list_users(
    request: Request,
    tenant_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    actor: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    scope_tenant, scope_roles = _scope_filter(actor)
    query = db.query(User)
    if scope_tenant is not None:
        query = query.filter(User.tenant_id == scope_tenant)
    elif tenant_id is not None:
        query = query.filter(User.tenant_id == tenant_id)
    if scope_roles is not None:
        query = query.filter(User.role.in_(scope_roles))
    if status in ("active", "disabled"):
        query = query.filter(User.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(User.email.ilike(like) | User.display_name.ilike(like))
    total = query.count()
    rows = (
        query.order_by(User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    names = _tenant_names(db)
    return {"items": [_user_dict(db, u, names) for u in rows], "total": total}


@router.post("/users", dependencies=[Depends(require_tenant_admin)])
def create_user(
    payload: dict, request: Request,
    actor: User = Depends(require_tenant_admin), db: Session = Depends(get_db),
):
    data = payload or {}
    email = _validate_email(str(data.get("email") or ""))
    password = _validate_password(str(data.get("password") or ""))
    role = str(data.get("role") or ROLE_MEMBER)
    if role not in ROLES:
        raise HTTPException(400, "角色只能是 super_admin / tenant_admin / member")

    if actor.role == ROLE_TENANT_ADMIN:
        if role != ROLE_MEMBER:
            raise HTTPException(403, "租户管理员只能创建普通成员")
        tenant_id = actor.tenant_id
    else:
        tenant_id = data.get("tenant_id")
        if not isinstance(tenant_id, int):
            raise HTTPException(400, "tenant_id 必填")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "租户不存在")

    if db.query(User).filter_by(email=email).first():
        raise HTTPException(400, "该邮箱已被使用")

    user = User(
        email=email,
        password_hash=passwords.hash_password(password),
        display_name=str(data.get("display_name") or "").strip()[:64],
        tenant_id=tenant_id,
        role=role,
        must_change_password=True,
        created_by=actor.email,
    )
    db.add(user)
    db.commit()
    # 审计只记账号信息，不记初始密码
    audit_service.record(db, "user.created", request=request, user=actor,
                         detail={"created_user_id": user.id, "email": user.email,
                                 "role": role, "tenant_id": tenant_id})
    return {"ok": True, "item": _user_dict(db, user, _tenant_names(db))}


@router.put("/users/{user_id}", dependencies=[Depends(require_tenant_admin)])
def update_user(
    user_id: int, payload: dict, request: Request,
    actor: User = Depends(require_tenant_admin), db: Session = Depends(get_db),
):
    target = _target_for(actor, db, user_id)
    data = payload or {}
    detail: dict = {"target_user_id": target.id, "changes": []}

    if "display_name" in data:
        target.display_name = str(data.get("display_name") or "").strip()[:64]
        detail["changes"].append("display_name")

    if "status" in data:
        status = str(data.get("status") or "")
        if status not in ("active", "disabled"):
            raise HTTPException(400, "status 只能是 active / disabled")
        if status != target.status:
            _guard_last_super(db, target, demoting=False, disabling=status == "disabled")
            target.status = status
            detail["changes"].append(f"status={status}")
            if status == "disabled":
                # 停用即时生效：吊销全部会话
                from app.auth import sessions as session_store
                session_store.revoke_user_sessions(db, target.id)

    if "role" in data and str(data["role"]) != target.role:
        if actor.role != ROLE_SUPER_ADMIN:
            raise HTTPException(403, "只有平台超管可以调整角色")
        role = str(data.get("role") or "")
        if role not in ROLES:
            raise HTTPException(400, "角色只能是 super_admin / tenant_admin / member")
        _guard_last_super(db, target, demoting=role != ROLE_SUPER_ADMIN, disabling=False)
        target.role = role
        detail["changes"].append(f"role={role}")

    if "tenant_id" in data and actor.role == ROLE_SUPER_ADMIN:
        new_tenant = data.get("tenant_id")
        if isinstance(new_tenant, int) and new_tenant != target.tenant_id:
            if db.get(Tenant, new_tenant) is None:
                raise HTTPException(404, "租户不存在")
            target.tenant_id = new_tenant
            detail["changes"].append(f"tenant_id={new_tenant}")
        elif not isinstance(new_tenant, int):
            raise HTTPException(400, "tenant_id 必须为整数")

    db.commit()
    action = "user.disabled" if "status=disabled" in detail["changes"] else "user.updated"
    audit_service.record(db, action, request=request, user=actor, detail=detail)
    return {"ok": True, "item": _user_dict(db, target, _tenant_names(db))}


@router.post("/users/{user_id}/password-reset", dependencies=[Depends(require_tenant_admin)])
def reset_password(
    user_id: int, payload: dict, request: Request,
    actor: User = Depends(require_tenant_admin), db: Session = Depends(get_db),
):
    target = _target_for(actor, db, user_id)
    password = _validate_password(str((payload or {}).get("password") or ""))
    target.password_hash = passwords.hash_password(password)
    target.must_change_password = True
    target.failed_attempts = 0
    target.locked_until = None
    db.commit()
    from app.auth import sessions as session_store
    session_store.revoke_user_sessions(db, target.id)
    # 审计只记动作，不记新密码
    audit_service.record(db, "user.password_reset", request=request, user=actor,
                         detail={"target_user_id": target.id, "email": target.email})
    return {"ok": True}
