"""用户/租户/认证域回归测试（2026-09-09 第五轮审计修复的配套用例）。

离线运行：python -m pytest tests/test_users_auth.py -q
覆盖：权限矩阵与越权 404 掩蔽、末位超管/停租户护栏、租户停用会话吊销、
登录锁定+解锁、IP 限速、空闲超时、token 后门边界、弱口令、首登改密门禁。
"""
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import text

from app.auth import sessions as session_store
from app.services.audit_service import client_ip
from conftest import PW, login, set_config

H = {"X-Requested-With": "XMLHttpRequest"}


def other_client():
    """第二个浏览器画像：独立 CookieJar（app 级依赖覆写由 client fixture 已注册）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def audit_rows(db, action: str, email: str | None = None):
    s = db.factory()
    try:
        rows = s.execute(
            text("SELECT user_email, action, detail FROM audit_log WHERE action = :a ORDER BY id"),
            {"a": action},
        ).all()
    finally:
        s.close()
    # SQLite raw SQL 下 JSON 列以字符串返回
    rows = [
        (r[0], r[1], json.loads(r[2]) if isinstance(r[2], str) else r[2])
        for r in rows
    ]
    if email is None:
        return rows
    return [r for r in rows if r[0] == email]


# ———————————————————— 登录与会话 ————————————————————

def test_login_success_and_me(client, seeded):
    r = login(client, seeded.emails["super"])
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "super_admin"
    me = client.get("/api/v2/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == seeded.emails["super"]


def test_login_failure_audit_reasons(client, db, seeded):
    assert login(client, seeded.emails["member"], "wrong-password!").status_code == 401
    assert login(client, "ghost@test.local", "wrong-password!").status_code == 401
    reasons = {r[2].get("reason") for r in audit_rows(db, "auth.login_failed")}
    assert {"bad_credentials", "unknown_email"} <= reasons


def test_csrf_missing_header_403(client, seeded):
    r = client.post(
        "/api/v2/admin/users",
        json={"email": "x@test.local", "password": PW, "role": "member"},
    )
    assert r.status_code == 403


def test_token_backdoor_rejected_outside_admin(client, db, seeded):
    set_config(db, "admin_token", "secret-token-123")
    h = {"X-Admin-Token": "secret-token-123"}
    # /api/v2/admin/* 内可用
    ok = client.get("/api/v2/admin/audit-actions", headers=h)
    assert ok.status_code == 200
    # 业务接口不再接受 token（收敛前的旧语义是全站等效超管）
    denied = client.get("/api/v2/hot/board", headers=h)
    assert denied.status_code == 401


# ———————————————————— 权限矩阵 ————————————————————

def test_member_cannot_access_admin_api(client, seeded):
    login(client, seeded.emails["member"])
    assert client.get("/api/v2/admin/users").status_code == 403
    assert client.get("/api/v2/admin/tenants").status_code == 403


def test_tenant_admin_scope_and_404_masking(client, seeded):
    login(client, seeded.emails["ta"])
    items = client.get("/api/v2/admin/users").json()["items"]
    # ta 只能看到本租户的 member（不含自己、不含其他租户/其他角色）
    assert {u["email"] for u in items} == {seeded.emails["member"]}
    # 跨租户目标一律 404（不暴露存在性）
    assert client.put(f"/api/v2/admin/users/{seeded.ids['member_b']}", json={"status": "disabled"},
                      headers=H).status_code == 404
    assert client.post(f"/api/v2/admin/users/{seeded.ids['member_b']}/unlock", headers=H).status_code == 404
    # 不能创建管理员
    r = client.post("/api/v2/admin/users", json={
        "email": "new-admin@test.local", "password": PW, "role": "tenant_admin"}, headers=H)
    assert r.status_code == 403


def test_superuser_global_scope(client, seeded):
    login(client, seeded.emails["super"])
    items = client.get("/api/v2/admin/users").json()["items"]
    assert len(items) == 4
    assert client.get("/api/v2/admin/tenants").status_code == 200


# ———————————————————— 创建用户 ————————————————————

def test_create_user_duplicate_and_bool_tenant_id(client, seeded):
    login(client, seeded.emails["super"])
    body = {"email": "dupe@test.local", "password": PW, "role": "member",
            "tenant_id": seeded.tids["a"]}
    assert client.post("/api/v2/admin/users", json=body, headers=H).status_code == 200
    assert client.post("/api/v2/admin/users", json=body, headers=H).status_code == 400
    bad = dict(body, email="other@test.local", tenant_id=True)  # JSON bool 曾被当成 1
    assert client.post("/api/v2/admin/users", json=bad, headers=H).status_code == 400


def test_create_user_weak_password_blocked(client, seeded):
    login(client, seeded.emails["super"])
    for weak in ("12345678", "password1", "qq123456"):
        r = client.post("/api/v2/admin/users", json={
            "email": "weak@test.local", "password": weak, "role": "member",
            "tenant_id": seeded.tids["a"]}, headers=H)
        assert r.status_code == 400, weak
    # 含邮箱本体
    r = client.post("/api/v2/admin/users", json={
        "email": "wang@test.local", "password": "wang@test88", "role": "member",
        "tenant_id": seeded.tids["a"]}, headers=H)
    assert r.status_code == 400


def test_cannot_create_into_disabled_tenant(client, db, seeded):
    login(client, seeded.emails["super"])
    client.put(f"/api/v2/admin/tenants/{seeded.tids['b']}", json={"status": "disabled"}, headers=H)
    r = client.post("/api/v2/admin/users", json={
        "email": "late@test.local", "password": PW, "role": "member",
        "tenant_id": seeded.tids["b"]}, headers=H)
    assert r.status_code == 400


# ———————————————————— 末位超管 / 停租户护栏 ————————————————————

def test_last_super_demote_and_disable_blocked(client, seeded):
    login(client, seeded.emails["super"])
    uid = seeded.ids["super"]
    assert client.put(f"/api/v2/admin/users/{uid}", json={"role": "member"}, headers=H).status_code == 400
    assert client.put(f"/api/v2/admin/users/{uid}", json={"status": "disabled"}, headers=H).status_code == 400


def test_demote_super_with_second_super_revokes_sessions(client, db, seeded):
    login(client, seeded.emails["super"])
    # 建第二个超管后再降级原超管：护栏放行
    r = client.post("/api/v2/admin/users", json={
        "email": "super2@test.local", "password": PW, "role": "super_admin",
        "tenant_id": seeded.tids["a"]}, headers=H)
    assert r.status_code == 200
    r = client.put(f"/api/v2/admin/users/{seeded.ids['super']}", json={"role": "member"}, headers=H)
    assert r.status_code == 200
    # 权限变更即时吊销会话
    assert client.get("/api/v2/auth/me").status_code == 401


def test_tenant_disable_guard_and_session_revocation(client, db, seeded):
    login(client, seeded.emails["super"])
    # 租户A 内有启用超管 → 禁止停用（平台自锁护栏）
    r = client.put(f"/api/v2/admin/tenants/{seeded.tids['a']}", json={"status": "disabled"}, headers=H)
    assert r.status_code == 400
    # 租户B 无超管 → 放行，且成员会话全部吊销
    m = other_client()
    assert login(m, seeded.emails["member_b"]).status_code == 200
    r = client.put(f"/api/v2/admin/tenants/{seeded.tids['b']}", json={"status": "disabled"}, headers=H)
    assert r.status_code == 200
    assert m.get("/api/v2/auth/me").status_code == 401  # 旧会话已吊销
    # 登录口明确拒绝（而非登录成功后处处 403）
    assert login(m, seeded.emails["member_b"]).status_code == 403


def test_tenant_migration_revokes_old_sessions(client, db, seeded):
    """租户迁移即时吊销旧会话（2026-09-09 审计修复：旧实现先赋值再比较恒 False，
    迁移租户后旧 token 一直可用）。"""
    login(client, seeded.emails["super"])
    m = other_client()
    assert login(m, seeded.emails["member"]).status_code == 200
    assert m.get("/api/v2/auth/me").status_code == 200
    r = client.put(
        f"/api/v2/admin/users/{seeded.ids['member']}",
        json={"tenant_id": seeded.tids["b"]},
        headers=H,
    )
    assert r.status_code == 200
    assert m.get("/api/v2/auth/me").status_code == 401  # 旧会话已吊销


# ———————————————————— 锁定与解锁 ————————————————————

def test_lockout_visibility_and_unlock(client, db, seeded):
    set_config(db, "login_max_attempts", "2")
    # 第 1 次失败未达阈值 → 401；第 2 次失败达到阈值 → 立即锁定 423
    assert login(client, seeded.emails["member"], "wrong-password!").status_code == 401
    assert login(client, seeded.emails["member"], "wrong-password!").status_code == 423

    login(client, seeded.emails["super"])
    row = next(u for u in client.get("/api/v2/admin/users").json()["items"]
               if u["id"] == seeded.ids["member"])
    assert row["locked"] is True and row["locked_until"]
    # 租管也可解锁本租户成员
    login(client, seeded.emails["ta"])
    assert client.post(f"/api/v2/admin/users/{seeded.ids['member']}/unlock", headers=H).status_code == 200
    # 解锁后原密码直接可用（无需重置）
    assert login(client, seeded.emails["member"]).status_code == 200


# ———————————————————— IP 限速 ————————————————————

def test_ip_throttle_429(client, db):
    set_config(db, "login_ip_throttle_max", "3")
    for _ in range(3):
        assert login(client, "ghost@test.local", "wrong-password!").status_code == 401
    r = login(client, "ghost@test.local", "wrong-password!")
    assert r.status_code == 429
    # 超限后携带正确密码的请求同样被拒（限速在口令校验之前）
    assert login(client, "someone@test.local", PW).status_code == 429
    rows = audit_rows(db, "auth.throttled")
    assert rows and rows[-1][2].get("retry_after") >= 1


# ———————————————————— 空闲超时 ————————————————————

def test_idle_session_timeout(client, db, seeded):
    login(client, seeded.emails["member"])
    set_config(db, "session_idle_hours", "24")
    s = db.factory()
    from app.models.user_session import UserSession

    row = s.query(UserSession).filter_by(user_id=seeded.ids["member"]).first()
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=25)
    s.commit()
    s.close()
    assert client.get("/api/v2/auth/me").status_code == 401


# ———————————————————— 改密 / 重置 / 停用 ————————————————————

def test_password_reset_revokes_and_forces_change(client, db, seeded):
    m = other_client()
    assert login(m, seeded.emails["member"]).status_code == 200
    login(client, seeded.emails["ta"])
    new_pw = "Fresh-Password-99"
    assert client.post(f"/api/v2/admin/users/{seeded.ids['member']}/password-reset",
                       json={"password": new_pw}, headers=H).status_code == 200
    assert m.get("/api/v2/auth/me").status_code == 401  # 旧会话吊销
    r = login(m, seeded.emails["member"], new_pw)
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    # 重置时弱口令/含邮箱本体的密码被拒
    bad = client.post(f"/api/v2/admin/users/{seeded.ids['member']}/password-reset",
                      json={"password": "member@test88"}, headers=H)
    assert bad.status_code == 400


def test_change_password_revokes_other_sessions(client, seeded):
    c1, c2 = other_client(), other_client()
    for c in (c1, c2):
        assert login(c, seeded.emails["member"]).status_code == 200
    r = c1.put("/api/v2/auth/me/password", json={"old_password": PW, "new_password": "Brand-New-Pw-77"},
               headers=H)
    assert r.status_code == 200
    assert c2.get("/api/v2/auth/me").status_code == 401
    assert c1.get("/api/v2/auth/me").status_code == 200


def test_disabled_user_login_still_generic_but_audited(client, db, seeded):
    login(client, seeded.emails["super"])
    assert client.put(f"/api/v2/admin/users/{seeded.ids['member']}", json={"status": "disabled"},
                      headers=H).status_code == 200
    # 对外保持防枚举的同一句话；审计 reason 区分 account_disabled
    r = login(client, seeded.emails["member"])
    assert r.status_code == 401 and r.json()["detail"] == "邮箱或密码错误"
    reasons = {row[2].get("reason") for row in audit_rows(db, "auth.login_failed",
                                                          email=seeded.emails["member"])}
    assert "account_disabled" in reasons


# ———————————————————— 单元：IP 信任链 ————————————————————

def test_client_ip_prefers_real_ip_then_last_xff_hop():
    from starlette.requests import Request as StarletteRequest

    def req(headers: list[tuple[bytes, bytes]]):
        return StarletteRequest({"type": "http", "headers": headers, "client": ("10.0.0.1", 1234)})

    assert client_ip(req([(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8"),
                          (b"x-real-ip", b"9.9.9.9")])) == "9.9.9.9"
    assert client_ip(req([(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")])) == "5.6.7.8"
    assert client_ip(req([])) == "10.0.0.1"
