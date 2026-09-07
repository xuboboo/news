"""认证 API：/api/v2/auth/*。登录/登出/当前用户/改密/我的会话。

无注册端点——账号全部由管理员在 /api/v2/admin/users 下创建。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.auth import service as auth_service
from app.auth import sessions as session_store
from app.auth.deps import require_json_mutation, require_user
from app.core.db import get_db
from app.models.user import User
from app.models.user_session import UserSession
from app.services import audit_service

router = APIRouter(prefix="/api/v2/auth", dependencies=[Depends(require_json_mutation)])


@router.post("/login")
def api_login(payload: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    user = auth_service.login(
        db, request, response,
        str((payload or {}).get("email") or ""),
        str((payload or {}).get("password") or ""),
    )
    return auth_service.user_profile(user)


@router.post("/logout", dependencies=[Depends(require_user)])
def api_logout(request: Request, response: Response, user: User = Depends(require_user), db: Session = Depends(get_db)):
    auth_service.logout(db, request, response, user)
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_user)])
def api_me(user: User = Depends(require_user)):
    return auth_service.user_profile(user)


@router.put("/me/password", dependencies=[Depends(require_user)])
def api_change_password(
    payload: dict,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    auth_service.change_password(
        db, request, user,
        str((payload or {}).get("old_password") or ""),
        str((payload or {}).get("new_password") or ""),
    )
    return {"ok": True, "must_change_password": False}


@router.get("/sessions", dependencies=[Depends(require_user)])
def api_my_sessions(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """当前账号的活跃会话列表（识别本人在其他设备的登录）。"""
    current = getattr(request.state, "current_session", None)
    rows = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(timezone.utc),
        )
        .order_by(UserSession.id.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "ip": r.ip,
                "user_agent": r.user_agent,
                "current": current is not None and r.id == current.id,
            }
            for r in rows
        ]
    }


@router.post("/sessions/revoke-others", dependencies=[Depends(require_user)])
def api_revoke_other_sessions(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """登出其他设备：保留当前会话，吊销本人全部其余会话。"""
    current = getattr(request.state, "current_session", None)
    count = session_store.revoke_user_sessions(
        db, user.id, except_session_id=current.id if current else None
    )
    audit_service.record(
        db, "auth.session_revoked", request=request, user=user,
        detail={"revoke_others": count},
    )
    return {"ok": True, "revoked": count}


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_user)])
def api_revoke_session(
    session_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """踢掉自己的指定会话；不能踢别人的。"""
    row = db.get(UserSession, session_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    current = getattr(request.state, "current_session", None)
    if current is not None and row.id == current.id:
        raise HTTPException(400, "不能吊销当前会话，请用退出登录")
    session_store.revoke_session(db, row)
    audit_service.record(
        db, "auth.session_revoked", request=request, user=user,
        detail={"session_id": row.id},
    )
    return {"ok": True}
