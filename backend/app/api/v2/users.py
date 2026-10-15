"""租户与用户管理 API：/api/v2/admin/tenants*、/api/v2/admin/users*。

权限规则：
- super_admin：全局管理租户与用户，可分配任意角色；
- tenant_admin：仅本租户内管理 member（不能跨租户、不能建/改管理员与超管），
  越权目标一律 404（不暴露存在性）；
- 保护：平台至少保留一个 active 的 super_admin。
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import require_json_mutation, require_super_admin, require_tenant_admin
from app.auth import passwords
from app.auth import sessions as session_store
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
MAX_PASSWORD_BYTES = 72  # bcrypt 输入上限，超长会被静默截断，显式拒绝


def _escape_like(term: str) -> str:
    """ilike 通配符转义：用户输入中的 % _ \\ 不改变匹配语义。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email(email: str) -> str:
    email = _norm_email(email)
    if not email or len(email) > 255 or not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    return email


def _validate_password(password: str, *, email: str = "") -> str:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(400, f"密码过长（最多 {MAX_PASSWORD_BYTES} 字节）")
    try:
        passwords.check_strength(password, email=email)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return password


def _is_int(v) -> bool:
    """JSON 里 true/false 也是 int 的子类，显式排除（tenant_id: true 曾被当成 1）。"""
    return isinstance(v, int) and not isinstance(v, bool)


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
    # Serialize demotions/disables across processes. The lock is transaction-scoped,
    # so the count and the subsequent user update form one protected operation.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 731904218})
    active_supers = (
        db.query(func.count(User.id))
        .filter(User.role == ROLE_SUPER_ADMIN, User.status == "active", User.id != target.id)
        .scalar() or 0
    )
    if active_supers == 0:
        raise HTTPException(400, "平台至少保留一个启用的超管账号")


def _user_dict(db: Session, u: User, tenant_names: dict[int, str] | None = None) -> dict:
    locked_until = session_store.as_aware(u.locked_until)
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "tenant_id": u.tenant_id,
        "tenant_name": (tenant_names or {}).get(u.tenant_id),
        "must_change_password": bool(u.must_change_password),
        "locked": bool(locked_until and locked_until > session_store._now()),
        "locked_until": u.locked_until.isoformat() if u.locked_until else None,
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
    try:
        db.commit()
    except IntegrityError:
        # 并发同名创建：先查后插的窗口内撞唯一约束，转 400 而非 500
        db.rollback()
        raise HTTPException(400, "租户名已存在")
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
            if status == "disabled":
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 731904218})
                # 平台自锁护栏：租户内还有启用的超管时禁止停用——否则唯一超管
                # 会把自己连人带管理面一起锁在门外（只能 CLI/token 应急恢复）
                supers_here = (
                    db.query(func.count(User.id))
                    .filter(
                        User.tenant_id == tenant.id,
                        User.role == ROLE_SUPER_ADMIN,
                        User.status == "active",
                    )
                    .scalar() or 0
                )
                if supers_here:
                    raise HTTPException(400, "该租户下仍有启用的平台超管，请先迁移超管账号再停用租户")
            tenant.status = status
            detail["changes"].append(f"status={status}")
    db.commit()
    if "status=disabled" in detail["changes"]:
        # 停用即时失效：吊销该租户全部会话。仅靠每请求的租户状态检查，重启用
        # 后停用前签发的会话会整体复活（含可能已泄露的令牌）
        detail["sessions_revoked"] = session_store.revoke_tenant_sessions(db, tenant.id)
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
        like = f"%{_escape_like(q.strip())}%"
        query = query.filter(User.email.ilike(like, escape="\\") | User.display_name.ilike(like, escape="\\"))
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
    password = _validate_password(str(data.get("password") or ""), email=email)
    role = str(data.get("role") or ROLE_MEMBER)
    if role not in ROLES:
        raise HTTPException(400, "角色只能是 super_admin / tenant_admin / member")

    if actor.role == ROLE_TENANT_ADMIN:
        if role != ROLE_MEMBER:
            raise HTTPException(403, "租户管理员只能创建普通成员")
        tenant_id = actor.tenant_id
    else:
        tenant_id = data.get("tenant_id")
        if not _is_int(tenant_id):
            raise HTTPException(400, "tenant_id 必填")
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "租户不存在")
    if tenant.status != "active":
        raise HTTPException(400, "租户已停用，不能向其中创建账号")

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
    try:
        db.commit()
    except IntegrityError:
        # 并发同邮箱创建撞唯一约束（check-then-insert 竞态），转 400 而非 500
        db.rollback()
        raise HTTPException(400, "该邮箱已被使用")
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

    # ———— 校验阶段：全部通过才落库（多字段更新保持原子性，避免前一字段已生效而后一字段 400） ————
    new_display = str(data["display_name"] or "").strip()[:64] if "display_name" in data else None
    new_status = data.get("status") if "status" in data else None
    if new_status is not None and new_status not in ("active", "disabled"):
        raise HTTPException(400, "status 只能是 active / disabled")
    new_role = data.get("role") if "role" in data else None
    if new_role is not None:
        if actor.role != ROLE_SUPER_ADMIN:
            raise HTTPException(403, "只有平台超管可以调整角色")
        if new_role not in ROLES:
            raise HTTPException(400, "角色只能是 super_admin / tenant_admin / member")
    new_tenant = data.get("tenant_id") if "tenant_id" in data else None
    if new_tenant is not None:
        if actor.role != ROLE_SUPER_ADMIN:
            raise HTTPException(403, "只有平台超管可以调整所属租户")
        if not _is_int(new_tenant):
            raise HTTPException(400, "tenant_id 必须为整数")
        tenant = db.get(Tenant, new_tenant)
        if tenant is None:
            raise HTTPException(404, "租户不存在")
        if tenant.status != "active":
            # 与 create_user 同口径：停用租户不能收编账号（否则用户立刻陷入
            # 「登录 403」且无任何操作提示）
            raise HTTPException(400, "目标租户已停用，不能将用户迁入")

    status_changing = new_status is not None and new_status != target.status
    role_changing = new_role is not None and new_role != target.role
    if status_changing or role_changing:
        _guard_last_super(
            db, target,
            demoting=role_changing and new_role != ROLE_SUPER_ADMIN,
            disabling=status_changing and new_status == "disabled",
        )

    # ———— 应用阶段 ————
    detail: dict = {"target_user_id": target.id, "changes": []}
    if new_display is not None:
        target.display_name = new_display
        detail["changes"].append("display_name")
    if status_changing:
        target.status = new_status
        detail["changes"].append(f"status={new_status}")
    if role_changing:
        target.role = new_role
        detail["changes"].append(f"role={new_role}")
    # 先存旧值再比较：赋值后 target.tenant_id 已等于 new_tenant，
    # 直接比较恒为 False，租户迁移永远不会吊销会话（2026-09-09 审计修复）
    tenant_changing = new_tenant is not None and new_tenant != target.tenant_id
    if tenant_changing:
        detail["changes"].append(f"tenant_id={target.tenant_id}->{new_tenant}")
        target.tenant_id = new_tenant
    db.commit()
    if status_changing and new_status == "disabled":
        # 停用即时生效：吊销全部会话
        session_store.revoke_user_sessions(db, target.id)
    if role_changing or tenant_changing:
        # 权限/归属变更即时吊销会话（OWASP：privilege change 须重换会话）。
        # 权限本身每请求实时查库无提权残留，吊销的目的是强制以新身份重新登录；
        # 降级超管/搬移租户后继续用旧会话操作不符合直觉且扩大误操作面
        detail["sessions_revoked"] = session_store.revoke_user_sessions(db, target.id)

    action = "user.disabled" if "status=disabled" in detail["changes"] else "user.updated"
    audit_service.record(db, action, request=request, user=actor, detail=detail)
    return {"ok": True, "item": _user_dict(db, target, _tenant_names(db))}


@router.post("/users/{user_id}/unlock", dependencies=[Depends(require_tenant_admin)])
def unlock_user(
    user_id: int, request: Request,
    actor: User = Depends(require_tenant_admin), db: Session = Depends(get_db),
):
    """解除登录失败锁定（清零失败计数与锁定时间），不动密码与会话。

    此前解锁只能等锁定自然到期或走密码重置（顺带强制改密，杀伤过大）。
    """
    target = _target_for(actor, db, user_id)
    locked_until = session_store.as_aware(target.locked_until)
    if locked_until is None or locked_until <= session_store._now():
        return {"ok": True, "locked": False}
    target.locked_until = None
    target.failed_attempts = 0
    db.commit()
    audit_service.record(db, "user.unlocked", request=request, user=actor,
                         detail={"target_user_id": target.id, "email": target.email})
    return {"ok": True, "locked": False}


@router.post("/users/{user_id}/password-reset", dependencies=[Depends(require_tenant_admin)])
def reset_password(
    user_id: int, payload: dict, request: Request,
    actor: User = Depends(require_tenant_admin), db: Session = Depends(get_db),
):
    target = _target_for(actor, db, user_id)
    password = _validate_password(str((payload or {}).get("password") or ""), email=target.email)
    target.password_hash = passwords.hash_password(password)
    target.must_change_password = True
    target.failed_attempts = 0
    target.locked_until = None
    db.commit()
    session_store.revoke_user_sessions(db, target.id)
    # 审计只记动作，不记新密码
    audit_service.record(db, "user.password_reset", request=request, user=actor,
                         detail={"target_user_id": target.id, "email": target.email})
    return {"ok": True}
