"""成本看板 API 域回归测试。

P1 回归教训：/test/llm 与 /test/embedding「测试连接」走真实供应商、消耗 token，
但从未落 ai_usage_log——看板成本系统性低估。本文件锁定：连通性测试成功 /
api_fail / parse_fail 三态都必须落一条 conn_test 账（token 与响应 usage 一致）。

离线运行：httpx.post 整体替换为桩响应；record_usage 的独立会话
（app.core.db.SessionLocal）指到 SQLite 测试库。走 conftest 的 client/seeded
夹具（FastAPI 依赖覆写 + 超管登录），与 test_ops_monitoring 同款。
"""
import pytest

from app.models.usage import AiUsageLog, ModelPrice
from app.services import usage_service

H = {"X-Requested-With": "XMLHttpRequest"}  # admin API 变更请求必带的 CSRF 头

LLM_BODY = {
    "choices": [{"message": {"content": "OK"}}],
    "usage": {
        "prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11,
        "prompt_tokens_details": {"cached_tokens": 4},
    },
}
EMBED_BODY = {
    "data": [{"embedding": [0.1] * 8}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
}


class StubResp:
    def __init__(self, status_code=200, body=None, json_exc=None):
        self.status_code = status_code
        self._body = body
        self._json_exc = json_exc
        self.text = "stub"

    def json(self):
        if self._json_exc:
            raise self._json_exc("not json")
        return self._body


@pytest.fixture()
def super_client(client, seeded):
    from conftest import login

    login(client, seeded.emails["super"])
    return client


@pytest.fixture()
def usage_in_testdb(monkeypatch, db):
    """record_usage 的独立会话指到测试库：不靠真实 postgres 即可全链路断言落账。"""
    monkeypatch.setattr("app.core.db.SessionLocal", db.factory)


@pytest.fixture()
def open_url_guard(monkeypatch):
    """SSRF 守卫私网豁免：测试环境 DNS 行为不可控（通配解析/离线），
    豁免后 URL 守卫只做格式校验，端点路径确定走到 httpx 桩。"""
    import types

    monkeypatch.setattr(
        "app.utils.url_guard.get_settings",
        lambda: types.SimpleNamespace(allow_private_ai_urls=True),
    )


LLM_PAYLOAD = {"base_url": "https://llm.example.test", "api_key": "k", "model": "glm-4-flash"}
EMBED_PAYLOAD = {"base_url": "https://emb.example.test", "api_key": "k", "model": "emb-v"}


def _logs(db):
    s = db.factory()
    try:
        return s.query(AiUsageLog).order_by(AiUsageLog.id).all()
    finally:
        s.close()


# ———————————————————— /test/llm 三态落账 ————————————————————

def test_llm_conn_success_records_usage(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    monkeypatch.setattr(
        "app.api.v2.admin.httpx.post",
        lambda *a, **kw: StubResp(body=LLM_BODY),
    )
    r = super_client.post("/api/v2/admin/test/llm", json=LLM_PAYLOAD, headers=H)
    assert r.status_code == 200

    rows = _logs(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.task_type == "conn_test"
    assert row.trigger == "manual"
    assert row.status == "success"
    assert (row.prompt_tokens, row.completion_tokens, row.cached_tokens, row.total_tokens) == (9, 2, 4, 11)
    assert row.latency_ms >= 0


def test_llm_conn_api_fail_records_usage(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    monkeypatch.setattr(
        "app.api.v2.admin.httpx.post",
        lambda *a, **kw: StubResp(status_code=500, body={"error": "boom"}),
    )
    r = super_client.post("/api/v2/admin/test/llm", json=LLM_PAYLOAD, headers=H)
    assert r.status_code == 502

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == "api_fail"
    assert "500" in (rows[0].error or "")
    # 错误响应无 usage → token 记 0（未成功生成不虚记）
    assert rows[0].total_tokens == 0


def test_llm_conn_parse_fail_records_usage(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    """HTTP 200 但响应结构异常：token 已消耗，必须按 parse_fail 入账。"""
    monkeypatch.setattr(
        "app.api.v2.admin.httpx.post",
        lambda *a, **kw: StubResp(status_code=200, body={"usage": dict(LLM_BODY["usage"])}),
    )
    r = super_client.post("/api/v2/admin/test/llm", json=LLM_PAYLOAD, headers=H)
    assert r.status_code == 502

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == "parse_fail"
    assert rows[0].total_tokens == 11  # 花掉的 token 不丢账


def test_llm_conn_unreachable_records_usage(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    import httpx as httpx_mod

    def _boom(*a, **kw):
        raise httpx_mod.ConnectError("refused")

    monkeypatch.setattr("app.api.v2.admin.httpx.post", _boom)
    r = super_client.post("/api/v2/admin/test/llm", json=LLM_PAYLOAD, headers=H)
    assert r.status_code == 502

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == "api_fail"
    assert "refused" in (rows[0].error or "")


# ———————————————————— /test/embedding 落账 ————————————————————

def test_embedding_conn_success_records_usage(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    monkeypatch.setattr(
        "app.api.v2.admin.httpx.post",
        lambda *a, **kw: StubResp(body=EMBED_BODY),
    )
    r = super_client.post("/api/v2/admin/test/embedding", json=EMBED_PAYLOAD, headers=H)
    assert r.status_code == 200

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].task_type == "conn_test"
    assert rows[0].status == "success"
    assert rows[0].total_tokens == 5


# ———————————————————— 价目 API 防呆 + 明细过滤 ————————————————————

def test_price_api_rejects_fat_finger(super_client):
    """手滑多打零（2 万/百万 token）必须 400，而不是悄悄按天价锁定之后每条流水。"""
    r = super_client.post(
        "/api/v2/admin/usage/prices",
        json={"model": "glm-4-flash", "price_input": 20000, "price_output": 0},
        headers=H,
    )
    assert r.status_code == 400
    assert "防呆上限" in r.json()["detail"]


def test_price_api_create_and_get(super_client, db):
    r = super_client.post(
        "/api/v2/admin/usage/prices",
        json={"model": "glm-4-flash", "price_input": 2, "price_output": 8, "price_cache": 1},
        headers=H,
    )
    assert r.status_code == 200
    assert r.json()["created"] is True
    s = db.factory()
    try:
        assert s.get(ModelPrice, "glm-4-flash").price_input == 2
    finally:
        s.close()
    assert super_client.get("/api/v2/admin/usage/prices").json()["items"][0]["model"] == "glm-4-flash"


def test_usage_logs_filter_accepts_conn_test(super_client, db, usage_in_testdb):
    """过滤白名单由 usage_service 常量拼装：conn_test 必须可过滤（防 pattern 写死回退）。"""
    s = db.factory()
    s.add(AiUsageLog(task_type="conn_test", trigger="manual", model="m", status="success"))
    s.commit()
    s.close()

    r = super_client.get("/api/v2/admin/usage/logs", params={"task_type": "conn_test"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["task_type"] == "conn_test"


def test_usage_summary_api_shape(super_client):
    """summary 响应含盲区告警与缓存分母新键（前端横幅/命中率依赖）。"""
    r = super_client.get("/api/v2/admin/usage/summary", params={"period": "30d"})
    assert r.status_code == 200
    body = r.json()
    assert "unpriced_models" in body
    assert "total_prompt" in body
    assert len(body["daily"]) == 30  # 恒 30 个整 UTC 日桶
    assert usage_service.LOG_TASK_TYPES == (
        "summary", "event_summary", "embedding", "report", "conn_test",
    )


# ———————————————————— 价目删除 ————————————————————

def test_price_delete_roundtrip(super_client, db):
    """DELETE：存在 → 200 + 行消失 + 审计留痕；再删 → 404。"""
    r = super_client.post(
        "/api/v2/admin/usage/prices",
        json={"model": "typo-model", "price_input": 1, "price_output": 2},
        headers=H,
    )
    assert r.status_code == 200

    r = super_client.delete("/api/v2/admin/usage/prices/typo-model", headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "typo-model"}
    s = db.factory()
    try:
        assert s.get(ModelPrice, "typo-model") is None
        import json as _json

        from app.models.audit_log import AuditLog

        audits = s.query(AuditLog).filter(AuditLog.action == "admin.price_deleted").all()
        assert len(audits) == 1
        assert _json.dumps(audits[0].detail, ensure_ascii=False).count("typo-model") >= 1
    finally:
        s.close()

    assert super_client.delete("/api/v2/admin/usage/prices/typo-model", headers=H).status_code == 404


def test_price_delete_reenables_unpriced_warning(super_client):
    """删除 0 价声明后盲区告警重新出现——价目删除的自愈可见性。"""
    super_client.post(
        "/api/v2/admin/usage/prices",
        json={"model": "self-hosted-x", "price_input": 0, "price_output": 0},
        headers=H,
    )
    r = super_client.get("/api/v2/admin/usage/summary", params={"period": "30d", "daily": 0, "groups": 0})
    assert r.json()["unpriced_models"] == []
    assert super_client.delete("/api/v2/admin/usage/prices/self-hosted-x", headers=H).status_code == 200


# ———————————————————— 导出带流水过滤 ————————————————————

def test_export_with_log_filters(super_client, db, usage_in_testdb):
    """导出透传明细页筛选：只导出命中 task_type+status 的行。"""
    s = db.factory()
    s.add(AiUsageLog(task_type="summary", trigger="auto", model="llm-a",
                     status="success", total_tokens=10))
    s.add(AiUsageLog(task_type="embedding", trigger="auto", model="emb-a",
                     status="api_fail", error="boom", total_tokens=20))
    s.commit()
    s.close()

    r = super_client.get(
        "/api/v2/admin/usage/export",
        params={"task_type": "embedding", "status": "api_fail"},
    )
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "emb-a" in text and "boom" in text
    assert "llm-a" not in text

    # 白名单外的过滤值 → 422（Query pattern 校验）
    assert (
        super_client.get("/api/v2/admin/usage/export", params={"task_type": "nope"}).status_code
        == 422
    )
