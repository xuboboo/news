"""验收 G：登录系统与多租户权限专项审计（权限矩阵 / 锁定 / 会话吊销 / CSRF / 审计回查）。

前置条件：
  后端已启动且 ADMIN_NO_AUTH=false；数据库已建表（backend 启动自动执行）。
  AUDIT_EMAIL / AUDIT_PASSWORD —— 一个「平台超管」账号（必需，跑全矩阵）。
  ADMIN_TOKEN —— 可选，提供则回归 X-Admin-Token 应急后门用例。

说明：测试账号固定使用 *@audit.local 邮箱，脚本幂等（已存在则重置密码复用）；
运行会写入真实的登录失败/锁定审计记录——这正是被验证的对象。

用法：
  AUDIT_EMAIL=admin@x.local AUDIT_PASSWORD=... python scripts/audit_auth.py
"""
import json
import os
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AUDIT_BASE", "http://127.0.0.1:18000/api/v2")
AUDIT_EMAIL = os.environ.get("AUDIT_EMAIL", "")
AUDIT_PASSWORD = os.environ.get("AUDIT_PASSWORD", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

results = []

MEMBER_EMAIL = "member@audit.local"
TA_EMAIL = "ta@audit.local"
TEST_PW = "audit-pw-2026A"


def record(name, cond, detail=""):
    results.append((name, "PASS" if cond else "FAIL", detail))


def make_client():
    """一个独立浏览器画像：自己的 CookieJar（互不共享会话）。"""
    jar = http.cookiejar.CookieJar()
    return jar, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(opener, name, path, expect=200, method="GET", body=None, headers=None):
    """发请求并断言状态码（expect 可为 int 或 int 元组）；返回 (status, json|None, headers)。"""
    url = BASE + urllib.parse.quote(path, safe="/?=&")
    hdrs = {"X-Requested-With": "XMLHttpRequest"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    for k, v in (headers or {}).items():
        hdrs[k] = v
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=20) as r:
            status, raw, rh = r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:
        status, raw, rh = e.code, e.read().decode("utf-8"), dict(e.headers)
    except Exception as e:
        status, raw, rh = 0, str(e), {}
    expected = expect if isinstance(expect, tuple) else (expect,)
    ok = status in expected
    try:
        data_json = json.loads(raw)
    except Exception:
        data_json = None
    record(f"{name} → {expected[0] if len(expected) == 1 else expected}", ok,
           "" if ok else f"实际 {status}: {raw[:150]}")
    return status, data_json, rh


def login(client, email, password, expect=200):
    return call(
        client[1], f"登录 {email}", "/auth/login", expect=expect, method="POST",
        body={"email": email, "password": password},
    )


def capture_set_cookie(opener, email, password):
    """登录并返回 Set-Cookie 原文（断言 Cookie 属性用）。

    注意用 HTTPMessage.get（大小写不敏感）：uvicorn 下发的是小写 set-cookie，
    dict(r.headers) 取键是大小写敏感的会拿不到。
    """
    url = BASE + "/auth/login"
    req = urllib.request.Request(
        url,
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Set-Cookie", "")


anon = make_client()      # 未登录画像
super_c = make_client()   # 平台超管
member_c = make_client()  # 普通成员
ta_c = make_client()      # 租户管理员
member2_c = make_client() # 成员的第二个会话（改密吊销用例）

# ———— 1. 公开探针与门禁 ————
call(anon[1], "health 公开", "/health")
call(anon[1], "匿名 stats → 401", "/stats", expect=401)
call(anon[1], "匿名热榜 → 401", "/hot/board", expect=401)
call(anon[1], "匿名资讯 → 401", "/articles/latest?limit=1", expect=401)
call(anon[1], "匿名事件 → 401", "/events", expect=401)
call(anon[1], "匿名日报 → 401", "/report/dates", expect=401)
call(anon[1], "匿名 admin/status → 401", "/admin/status", expect=401)
call(anon[1], "匿名用户列表 → 401", "/admin/users", expect=401)
call(anon[1], "匿名审计 → 401", "/admin/audit-logs", expect=401)

# ———— 2. CSRF 防线（未携带 X-Requested-With / 非 JSON） ————
url = BASE + "/auth/login"
req = urllib.request.Request(url, data=json.dumps({"email": "x@y.z", "password": "p"}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        s = r.status
except urllib.error.HTTPError as e:
    s = e.code
record("login 无 X-Requested-With → 403", s == 403, f"实际 {s}")

req = urllib.request.Request(url, data="email=x%40y.z&password=p".encode(),
                             headers={"Content-Type": "application/x-www-form-urlencoded",
                                      "X-Requested-With": "XMLHttpRequest"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        s = r.status
except urllib.error.HTTPError as e:
    s = e.code
record("login urlencoded → 415", s == 415, f"实际 {s}")

if not (AUDIT_EMAIL and AUDIT_PASSWORD):
    print("\n===== 未配置 AUDIT_EMAIL / AUDIT_PASSWORD，仅完成匿名与 CSRF 用例 =====")
    passed = sum(1 for _, v, _ in results if v == "PASS")
    failed = len(results) - passed
    for name, v, detail in results:
        if v == "FAIL":
            print(f"FAIL | {name} {detail}")
    print(f"PASS {passed} / FAIL {failed} / TOTAL {len(results)}")
    raise SystemExit(1 if failed else 0)

# ———— 3. 登录行为 ————
login(anon, AUDIT_EMAIL, "wrong-password", expect=401)          # 错误密码
s, sc = capture_set_cookie(super_c[1], AUDIT_EMAIL, AUDIT_PASSWORD)
record("登录成功 → 200", s == 200, f"实际 {s}")
low = (sc or "").lower()
record("Cookie 属性 HttpOnly", "httponly" in low, sc[:120] or "无 Set-Cookie")
record("Cookie 属性 SameSite=lax", "samesite=lax" in low, sc[:120])
record("Cookie 属性 Path=/", "path=/" in low, sc[:120])
call(super_c[1], "/me 返回角色", "/auth/me")
# /me 角色断言在下一行 check（依赖上一行返回值，单独 call 一次拿数据）
_, me_data, _ = call(super_c[1], "/me 数据", "/auth/me")
record("当前账号是超管", bool(me_data and me_data.get("role") == "super_admin"),
       str(me_data and me_data.get("role")))
SUPER_UID = (me_data or {}).get("id")

# ———— 4. 会话访问数据端点（门禁放行） ————
call(super_c[1], "会话热榜 → 200", "/hot/board")
call(super_c[1], "会话统计 → 200", "/stats")
call(super_c[1], "会话日报 → 200", "/report/dates")

# ———— 5. 造测试租户/账号（幂等：已存在则重置复用） ————
_, t_data, _ = call(super_c[1], "建测试租户(或已存在)", "/admin/tenants", method="POST",
                    body={"name": "审计测试租户", "remark": "audit_auth.py"}, expect=(200, 400))
tenant_id = (t_data or {}).get("item", {}).get("id")
if not tenant_id:
    _, tl, _ = call(super_c[1], "取租户列表", "/admin/tenants")
    tenant_id = next((t["id"] for t in (tl or {}).get("items", []) if t["name"] == "审计测试租户"), None)
record("测试租户就绪", tenant_id is not None, f"tenant_id={tenant_id}")


def ensure_user(email, role, password=TEST_PW, tenant=None):
    """创建或重置测试账号；返回 user_id。（email 交给 call() 统一做 URL 编码，勿提前 quote）"""
    tid = tenant if tenant is not None else tenant_id
    _, d, _ = call(super_c[1], f"建账号 {email}", "/admin/users", method="POST",
                   body={"email": email, "password": password, "role": role,
                         "tenant_id": tid, "display_name": "审计测试"},
                   expect=(200, 400))
    uid = (d or {}).get("item", {}).get("id")
    if uid:
        return uid
    # 已存在：查列表拿 id，重置密码 + 启用 + 校正角色
    _, ul, _ = call(super_c[1], f"查账号 {email}", f"/admin/users?q={email}")
    uid = next((u["id"] for u in (ul or {}).get("items", []) if u["email"] == email), None)
    if not uid:
        return None
    call(super_c[1], f"重置 {email}", f"/admin/users/{uid}/password-reset", method="POST",
         body={"password": password})
    call(super_c[1], f"启用 {email}", f"/admin/users/{uid}", method="PUT", body={"status": "active"})
    call(super_c[1], f"校正角色 {email}", f"/admin/users/{uid}", method="PUT", body={"role": role})
    return uid


def clear_must_change(client, email, password):
    """同值改密清除首登强制改密标记：服务端守卫会拦 must_change 用户的非认证请求。"""
    call(client[1], f"解除改密标记 {email}", "/auth/me/password", expect=200, method="PUT",
         body={"old_password": password, "new_password": password})


member_id = ensure_user(MEMBER_EMAIL, "member")
ta_id = ensure_user(TA_EMAIL, "tenant_admin")
record("测试账号就绪", member_id is not None and ta_id is not None)
if member_id is None or ta_id is None:
    print("FATAL: 测试账号未能创建/复用，中止（检查上面建账号用例的失败原因）")
    raise SystemExit(1)

# ———— 6. 权限矩阵 ————
login(member_c, MEMBER_EMAIL, TEST_PW)
login(ta_c, TA_EMAIL, TEST_PW)
# 重置过的账号 must_change_password=True，服务端守卫会拦非认证请求：先同值改密解除
clear_must_change(member_c, MEMBER_EMAIL, TEST_PW)
clear_must_change(ta_c, TA_EMAIL, TEST_PW)

call(member_c[1], "member 热榜 → 200", "/hot/board")
call(member_c[1], "member 用户列表 → 403", "/admin/users", expect=403)
call(member_c[1], "member 审计 → 403", "/admin/audit-logs", expect=403)
call(member_c[1], "member admin/status → 403", "/admin/status", expect=403)
call(member_c[1], "member 建租户 → 403", "/admin/tenants", expect=403, method="POST", body={"name": "x"})
call(member_c[1], "member 改配置 → 403", "/admin/config", expect=403, method="PUT", body={})

_, ul, _ = call(ta_c[1], "ta 用户列表 → 200", "/admin/users")
ta_sees = [u["email"] for u in (ul or {}).get("items", [])]
record("ta 只见本租户 member", MEMBER_EMAIL in ta_sees and AUDIT_EMAIL not in ta_sees
       and TA_EMAIL not in ta_sees, f"可见={ta_sees}")
call(ta_c[1], "ta 审计 → 403", "/admin/audit-logs", expect=403)
call(ta_c[1], "ta admin/status → 403", "/admin/status", expect=403)
call(ta_c[1], "ta 建管理员 → 403", "/admin/users", expect=403, method="POST",
     body={"email": "evil@audit.local", "password": TEST_PW, "role": "tenant_admin", "tenant_id": tenant_id})
call(ta_c[1], "ta 改超管账号 → 404", f"/admin/users/{SUPER_UID}", expect=404, method="PUT",
     body={"display_name": "hacked"})
call(ta_c[1], "ta 停用他租户账号 → 404", f"/admin/users/{member_id + 1000}", expect=404, method="PUT",
     body={"status": "disabled"})

# ———— 7. 登录失败锁定 ————
for i in range(1, 5):
    login(anon, MEMBER_EMAIL, "wrong", expect=401)
login(anon, MEMBER_EMAIL, "wrong", expect=423)          # 第 5 次：触发锁定
login(member_c, MEMBER_EMAIL, TEST_PW, expect=423)      # 锁定窗口内正确密码仍拒
# 超管重置解锁
call(super_c[1], "超管重置解锁", f"/admin/users/{member_id}/password-reset", method="POST",
     body={"password": TEST_PW})
login(member_c, MEMBER_EMAIL, TEST_PW)                  # 解锁后可登录

# ———— 8. 改密吊销其他会话 ————
login(member2_c, MEMBER_EMAIL, TEST_PW)                 # 第二个会话
call(member_c[1], "改密成功", "/auth/me/password", expect=200, method="PUT",
     body={"old_password": TEST_PW, "new_password": "audit-pw-new45"})
call(member2_c[1], "旧会话已吊销 → 401", "/auth/me", expect=401)
call(member_c[1], "改密后改回", "/auth/me/password", expect=200, method="PUT",
     body={"old_password": "audit-pw-new45", "new_password": TEST_PW})

# ———— 8b. 登出其他设备（revoke-others：保留当前会话） ————
login(member2_c, MEMBER_EMAIL, TEST_PW)
call(member_c[1], "登出其他设备 → 200", "/auth/sessions/revoke-others", expect=200, method="POST", body={})
call(member2_c[1], "其他设备已吊销 → 401", "/auth/me", expect=401)
call(member_c[1], "当前会话保留 → 200", "/auth/me")

# ———— 8c. 密码长度上限（bcrypt 72 字节截断防护） ————
call(member_c[1], "超长密码 → 400", "/auth/me/password", expect=400, method="PUT",
     body={"old_password": TEST_PW, "new_password": "x" * 80})

# ———— 8d. 租户停用：登录口明确拒绝（缺陷 A 回归：不得出现"登录成功但全站 403"） ————
call(super_c[1], "建停用测试租户", "/admin/tenants", method="POST",
     body={"name": "停用租户审计", "remark": "audit_auth.py"}, expect=(200, 400))
_, tl, _ = call(super_c[1], "取停用测试租户", "/admin/tenants")
dt_id = next((t["id"] for t in (tl or {}).get("items", []) if t["name"] == "停用租户审计"), None)
record("停用测试租户就绪", dt_id is not None)
if dt_id is not None:
    call(super_c[1], "停用该租户", f"/admin/tenants/{dt_id}", method="PUT", body={"status": "disabled"})
    dt_uid = ensure_user("dt@audit.local", "member", tenant=dt_id)
    if dt_uid:
        login(anon, "dt@audit.local", TEST_PW, expect=403)

# ———— 9. 登出吊销 ————
call(ta_c[1], "ta 登出", "/auth/logout", expect=200, method="POST", body={})
call(ta_c[1], "登出后会话失效 → 401", "/auth/me", expect=401)

# ———— 10. X-Admin-Token 后门回归（可选） ————
if ADMIN_TOKEN:
    url = BASE + "/admin/status"
    req = urllib.request.Request(url, headers={"X-Admin-Token": ADMIN_TOKEN}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            s = r.status
    except urllib.error.HTTPError as e:
        s = e.code
    record("后门 X-Admin-Token → 200", s == 200, f"实际 {s}")
else:
    record("ADMIN_TOKEN 未设置，跳过后门用例", True, "设 ADMIN_TOKEN 环境变量后重跑")

# ———— 11. 审计回查：上面的行为都有留痕 ————
_, al, _ = call(super_c[1], "审计查询-登录失败", "/admin/audit-logs?action=auth.login_failed&page_size=5")
record("审计有「登录失败」记录", bool(al and al.get("total", 0) > 0), f"total={al and al.get('total')}")
_, al, _ = call(super_c[1], "审计查询-锁定", "/admin/audit-logs?action=auth.locked&page_size=5")
record("审计有「账号锁定」记录", bool(al and al.get("total", 0) > 0), f"total={al and al.get('total')}")
_, al, _ = call(super_c[1], "审计查询-登录成功", "/admin/audit-logs?action=auth.login&page_size=5")
first = ((al or {}).get("items") or [{}])[0]
record("审计登录记录含 IP/UA", bool(first.get("ip") and first.get("user_agent")),
       f"ip={first.get('ip')} ua={bool(first.get('user_agent'))}")
_, al, _ = call(super_c[1], "审计查询-重置密码", "/admin/audit-logs?action=user.password_reset&page_size=5")
record("审计有「重置密码」记录", bool(al and al.get("total", 0) > 0), f"total={al and al.get('total')}")

# 汇总
print("\n===== 鉴权与权限验收结果 =====")
passed = sum(1 for _, v, _ in results if v == "PASS")
failed = len(results) - passed
for name, v, detail in results:
    if v == "FAIL":
        print(f"FAIL | {name} {detail}")
print(f"PASS {passed} / FAIL {failed} / TOTAL {len(results)}")
raise SystemExit(1 if failed else 0)
