"""管理端「恢复默认」配置重置端点回归测试（POST /admin/config/reset）。

锁定五件事：
1. 整组重置删除 DB 覆盖：生效值回落 .env 默认（LLM 默认空 = 内置抽取）；
2. 幂等：无覆盖的 key 静默跳过（reset 为空列表也是 200）；
3. 参数防线：未知 key / 空 keys → 400；
4. 鉴权：member 角色与未登录一律拒绝；
5. 审计：admin.config_reset 单独留痕，记录实际删除与跳过的 key。

走 conftest 的 client/seeded 夹具（FastAPI 依赖覆写 + 超管登录），
与 test_usage_api 同款。
"""
from conftest import login

H = {"X-Requested-With": "XMLHttpRequest"}  # admin API 变更请求必带的 CSRF 头

LLM_KEYS = ["llm_base_url", "llm_api_key", "llm_model", "llm_protocol"]


def _login_super(client, seeded):
    r = login(client, seeded.emails["super"])
    assert r.status_code == 200


def test_reset_clears_group_overrides(client, seeded):
    """写覆盖 → 整组恢复 → 覆盖消失、生效值回落 .env 默认（LLM 为空 = 未配置）。"""
    _login_super(client, seeded)
    r = client.put(
        "/api/v2/admin/config",
        json={k: v for k, v in zip(LLM_KEYS, ["https://x.test/v1", "sk-x", "m-x", "anthropic"])},
        headers=H,
    )
    assert r.status_code == 200
    assert all(r.json()["config"][k]["source"] == "db" for k in LLM_KEYS)

    r = client.post("/api/v2/admin/config/reset", json={"keys": LLM_KEYS}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["reset"]) == sorted(LLM_KEYS)
    assert all(body["config"][k]["source"] == "env" for k in LLM_KEYS)
    assert all(body["config"][k]["overridden"] is False for k in LLM_KEYS)

    # 生效值核对：测试环境 .env 无 LLM 键 → 全部回落空（内置抽取），协议回落 openai
    from app.core.db import get_db
    from app.services import config_service

    db = next(client.app.dependency_overrides[get_db]())
    assert config_service.effective_llm(db) == ("", "", "", "openai")


def test_reset_is_idempotent_when_no_overrides(client, seeded):
    """无覆盖时整组重置：200 + reset 空列表（幂等，可重复点）。"""
    _login_super(client, seeded)
    r = client.post("/api/v2/admin/config/reset", json={"keys": LLM_KEYS}, headers=H)
    assert r.status_code == 200
    assert r.json()["reset"] == []


def test_reset_rejects_unknown_keys(client, seeded):
    _login_super(client, seeded)
    r = client.post(
        "/api/v2/admin/config/reset",
        json={"keys": ["llm_base_url", "not_a_key"]},
        headers=H,
    )
    assert r.status_code == 400
    assert "not_a_key" in r.json()["detail"]


def test_reset_rejects_empty_keys(client, seeded):
    _login_super(client, seeded)
    for payload in ({}, {"keys": []}):
        r = client.post("/api/v2/admin/config/reset", json=payload, headers=H)
        assert r.status_code == 400


def test_reset_requires_super_admin(client, seeded):
    """member 与未登录一律拒绝（越权 403 由 main.py 审计处理器留痕）。"""
    login(client, seeded.emails["member"])
    r = client.post("/api/v2/admin/config/reset", json={"keys": LLM_KEYS}, headers=H)
    assert r.status_code == 403

    client.cookies.clear()  # 清掉 member 会话，验证真·未登录
    r = client.post("/api/v2/admin/config/reset", json={"keys": LLM_KEYS}, headers=H)
    assert r.status_code == 401


def test_reset_writes_distinct_audit_action(client, seeded):
    """重置以 admin.config_reset 单独留痕（与逐项编辑的 config_updated 可区分）。"""
    _login_super(client, seeded)
    client.put("/api/v2/admin/config", json={"llm_model": "m-x"}, headers=H)
    r = client.post(
        "/api/v2/admin/config/reset",
        json={"keys": ["llm_model", "llm_base_url"]},  # 后者无覆盖 → 进 skipped
        headers=H,
    )
    assert r.status_code == 200

    from app.core.db import get_db

    db = next(client.app.dependency_overrides[get_db]())
    from app.models.audit_log import AuditLog

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "admin.config_reset")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    detail = str(row.detail)
    assert "llm_model" in detail and "llm_base_url" in detail
