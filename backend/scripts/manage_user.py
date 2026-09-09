"""运维 CLI：建超管 / 重置密码 / 启停用户（忘记密码或无引导环境时的自救入口）。

用法（在 backend 容器内或本机 backend/ 目录）：
  python scripts/manage_user.py create-super <email> [password|-] [显示名]
  python scripts/manage_user.py reset-password <email> [password|-]
  python scripts/manage_user.py set-status <email> active|disabled
  python scripts/manage_user.py list

密码传参：省略 = 交互式输入（不回显，不进 shell 历史）；传 "-" = 从 stdin 读一行。
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func  # noqa: E402

from app.auth import passwords  # noqa: E402
from app.auth import sessions as session_store  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: F401,E402 显式注册（漏 import 会静默缺表）
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
from app.models.tenant import Tenant  # noqa: E402
from app.models.user import ROLE_SUPER_ADMIN, User  # noqa: E402


def _ensure_tables() -> None:
    """独立运行时兜底建表（与 init_db.py 同语义；已存在则无操作）。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        # article 含 Vector 列、标题 trgm 索引依赖 pg_trgm：全新库缺扩展会直接建表失败
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _read_password(arg: str | None, prompt: str) -> str:
    """密码来源优先级：显式参数 > "-" 读 stdin > 交互式输入（不回显）。

    避免口令经 argv 暴露在 shell 历史与进程列表里。"""
    if arg == "-":
        return sys.stdin.readline().rstrip("\r\n")
    if arg:
        return arg
    pw = getpass.getpass(f"{prompt}（输入不回显）: ")
    if not pw:
        sys.exit("密码不能为空")
    return pw


def _ensure_len(pw: str) -> str:
    if len(pw) < 8:
        sys.exit("密码至少 8 位")
    return pw


def _ensure_strength(pw: str, email: str = "") -> str:
    """CLI 建号/重置同样过弱口令封锁表与邮箱本体校验（与 API 三条写路径同口径）。"""
    _ensure_len(pw)
    try:
        passwords.check_strength(pw, email=email)
    except ValueError as exc:
        sys.exit(str(exc))
    return pw


def create_super(email: str, password: str | None, display_name: str) -> None:
    email = _norm(email)
    password = _ensure_strength(_read_password(password, "超管初始密码"), email)
    with SessionLocal() as db:
        if db.query(func.count(User.id)).filter(User.role == ROLE_SUPER_ADMIN).scalar():
            sys.exit("已存在超管账号（避免误用此命令创建第二个超管；如确需请用 SQL）")
        # get-or-create：与 main.bootstrap_admin 同口径，重名复用既有"默认租户"
        tenant = db.query(Tenant).filter_by(name="默认租户").first()
        if tenant is None:
            tenant = Tenant(name="默认租户", remark="manage_user.py 引导创建")
            db.add(tenant)
            db.flush()
        user = User(
            email=email,
            password_hash=passwords.hash_password(password),
            display_name=display_name or "平台超管",
            tenant_id=tenant.id,
            role=ROLE_SUPER_ADMIN,
            must_change_password=True,
            created_by="manage_user.py",
        )
        db.add(user)
        db.commit()
        print(f"已创建超管 {email}（首次登录需修改密码）")


def reset_password(email: str, password: str | None) -> None:
    password = _ensure_strength(_read_password(password, f"{_norm(email)} 的新密码"), _norm(email))
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=_norm(email)).first()
        if user is None:
            sys.exit(f"用户不存在：{email}")
        user.password_hash = passwords.hash_password(password)
        user.must_change_password = True
        user.failed_attempts = 0
        user.locked_until = None
        db.commit()
        revoked = session_store.revoke_user_sessions(db, user.id)
        print(f"已重置 {user.email} 的密码（首登强制改密；吊销 {revoked} 个会话）")


def set_status(email: str, status: str) -> None:
    if status not in ("active", "disabled"):
        sys.exit("status 只能是 active / disabled")
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=_norm(email)).first()
        if user is None:
            sys.exit(f"用户不存在：{email}")
        user.status = status
        db.commit()
        if status == "disabled":
            session_store.revoke_user_sessions(db, user.id)
        print(f"{user.email} → {status}")


def list_users() -> None:
    with SessionLocal() as db:
        tenants = {t.id: t.name for t in db.query(Tenant).all()}
        for u in db.query(User).order_by(User.id).all():
            print(
                f"#{u.id} {u.email} role={u.role} tenant={tenants.get(u.tenant_id, u.tenant_id)} "
                f"status={u.status} must_change_password={bool(u.must_change_password)}"
            )


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    if cmd == "create-super" and len(rest) >= 1:
        create_super(rest[0], rest[1] if len(rest) > 1 else None,
                     rest[2] if len(rest) > 2 else "")
    elif cmd == "reset-password" and len(rest) >= 1:
        reset_password(rest[0], rest[1] if len(rest) > 1 else None)
    elif cmd == "set-status" and len(rest) == 2:
        set_status(rest[0], rest[1])
    elif cmd == "list":
        list_users()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    _ensure_tables()
    main()
