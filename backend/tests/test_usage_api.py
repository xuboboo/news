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


# ———————————————————— /test/llm anthropic 原生协议 ————————————————————

ANTHROPIC_BODY = {
    "content": [{"type": "text", "text": "OK"}],
    "usage": {"input_tokens": 12, "cache_read_input_tokens": 3, "output_tokens": 2},
}


def test_llm_conn_anthropic_protocol_success(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    """protocol=anthropic：请求打 /v1/messages + x-api-key，usage 归一化后落同一本账。"""
    captured = {}

    def fake_post(url, *a, **kw):
        captured["url"] = url
        captured["headers"] = kw.get("headers") or {}
        return StubResp(body=ANTHROPIC_BODY)

    monkeypatch.setattr("app.api.v2.admin.httpx.post", fake_post)
    payload = {**LLM_PAYLOAD, "base_url": "https://relay.example.test/anthropic", "protocol": "anthropic"}
    r = super_client.post("/api/v2/admin/test/llm", json=payload, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "OK"

    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"].get("x-api-key") == "k"
    assert "Authorization" not in captured["headers"]

    rows = _logs(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "success"
    # input 12 + cache_read 3 = prompt 15；cached 3；total 17
    assert (row.prompt_tokens, row.completion_tokens, row.cached_tokens, row.total_tokens) == (15, 2, 3, 17)


def test_llm_conn_rejects_unknown_protocol(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    monkeypatch.setattr(
        "app.api.v2.admin.httpx.post",
        lambda *a, **kw: pytest.fail("未知协议不应发出请求"),
    )
    payload = {**LLM_PAYLOAD, "protocol": "gemini"}
    r = super_client.post("/api/v2/admin/test/llm", json=payload, headers=H)
    assert r.status_code == 400
    assert _logs(db) == []


def test_llm_conn_invalid_base_url_returns_502(super_client, monkeypatch, usage_in_testdb, open_url_guard, db):
    """坏 base_url（非法端口）→ InvalidURL 非 HTTPError 子类，漏接会变结构性 500；
    必须归 api_fail 并以 502 透出（2026-09-12 审计修复回归）。"""
    import httpx as httpx_mod

    def _boom(url, *a, **kw):
        raise httpx_mod.InvalidURL("Invalid port: 'notaport'")

    monkeypatch.setattr("app.api.v2.admin.httpx.post", _boom)
    payload = {**LLM_PAYLOAD, "base_url": "https://host:notaport/"}
    r = super_client.post("/api/v2/admin/test/llm", json=payload, headers=H)
    assert r.status_code == 502

    rows = _logs(db)
    assert len(rows) == 1
    assert rows[0].status == "api_fail"
    assert "Invalid port" in (rows[0].error or "")


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
    """summary 响应含盲区告警/缓存分母/当日预算键（前端横幅/命中率/预算进度依赖）。"""
    r = super_client.get("/api/v2/admin/usage/summary", params={"period": "30d"})
    assert r.status_code == 200
    body = r.json()
    assert "unpriced_models" in body
    assert "total_prompt" in body
    assert "daily_budget" in body  # 面板预算随汇总返回（0 = 未启用）
    assert len(body["daily"]) == 30  # 恒 30 个整北京时间日桶
    assert usage_service.LOG_TASK_TYPES == (
        "summary", "event_summary", "embedding", "report", "conn_test",
    )


def test_usage_logs_filter_by_model(super_client, db):
    """model 精确过滤（Query max_length=64；盲区告警点击联动）。"""
    s = db.factory()
    s.add(AiUsageLog(task_type="summary", trigger="auto", model="llm-x", status="success"))
    s.add(AiUsageLog(task_type="summary", trigger="auto", model="llm-y", status="success"))
    s.commit()
    s.close()

    r = super_client.get("/api/v2/admin/usage/logs", params={"model": "llm-x"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["model"] == "llm-x"


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


def test_local_embedding_model_not_flagged_unpriced(super_client, usage_in_testdb, db):
    """本地自托管 embedding（bge-small-zh-v1.5）不进盲区告警：自托管无按量账单，
    cost=0 是设计使然，留在盲区清单会让面板长期挂假告警（2026-09-12 审计）。"""
    from app.services.usage_service import LOCAL_MODELS

    assert "BAAI/bge-small-zh-v1.5" in LOCAL_MODELS

    s = db.factory()
    s.add(AiUsageLog(task_type="embedding", trigger="auto", model="BAAI/bge-small-zh-v1.5",
                     status="success", prompt_tokens=600, total_tokens=600))
    s.commit()
    s.close()

    r = super_client.get("/api/v2/admin/usage/summary", params={"period": "30d", "daily": 0, "groups": 1})
    assert r.status_code == 200
    assert r.json()["unpriced_models"] == []


def test_seed_model_prices_idempotent(db):
    """价目种子幂等：首跑建价目，重跑不覆盖（面板手工调过的价不被冲掉）。"""
    import importlib

    from app.models.usage import ModelPrice

    seed = importlib.import_module("scripts.seed_model_prices")

    s = db.factory()
    try:
        seed_main = seed.main
        # 首跑： SessionLocal 指向真实 postgres，不能直接跑——改为逐模型断言种子常量
        # 与换算口径（脚本在容器内对真实库执行；此处锁「不覆盖已有价目」的语义）
        for model, pin, pout, pcache in seed.SEED_PRICES:
            s.add(ModelPrice(model=model, price_input=999, price_output=999, price_cache=999))
        s.commit()

        # 模拟 main() 的「已有则跳过」分支
        for model, pin, pout, pcache in seed.SEED_PRICES:
            row = s.get(ModelPrice, model)
            assert row is not None and float(row.price_input) == 999  # 未被覆盖

        # 换算口径锁死：deepseek-flash 高峰价 $0.30/$1.20/$0.006 × 7.2
        ds = [p for p in seed.SEED_PRICES if p[0] == "deepseek-flash"]
        assert ds and abs(ds[0][1] - 2.16) < 1e-9 and abs(ds[0][2] - 8.64) < 1e-9
    finally:
        s.close()


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


# ———————————————————— /usage/logs 清空 ————————————————————

def test_clear_usage_logs_deletes_all(super_client, usage_in_testdb, db):
    """清空返回删除行数：流水归零、明细接口同步为空；价目表不受影响。"""
    s = db.factory()
    for i in range(3):
        s.add(AiUsageLog(task_type="summary", trigger="auto", model=f"m-{i}",
                         status="success", total_tokens=10))
    s.add(ModelPrice(model="m-x", price_input=1, price_output=2))
    s.commit()
    s.close()

    r = super_client.delete("/api/v2/admin/usage/logs", headers=H)
    assert r.status_code == 200
    assert r.json()["deleted"] == 3

    s = db.factory()
    try:
        assert s.query(AiUsageLog).count() == 0
        assert s.query(ModelPrice).count() == 1  # 价目表不受清空影响
    finally:
        s.close()

    r = super_client.get("/api/v2/admin/usage/logs")
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_clear_usage_logs_writes_audit(super_client, usage_in_testdb, db):
    """清空以 admin.usage_logs_cleared 单独留痕，记录删除行数。"""
    s = db.factory()
    s.add(AiUsageLog(task_type="summary", trigger="auto", model="m",
                     status="success", total_tokens=10))
    s.commit()
    s.close()

    r = super_client.delete("/api/v2/admin/usage/logs", headers=H)
    assert r.status_code == 200

    from app.models.audit_log import AuditLog

    s = db.factory()
    try:
        row = (
            s.query(AuditLog)
            .filter(AuditLog.action == "admin.usage_logs_cleared")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None and row.detail["deleted"] == 1
    finally:
        s.close()


def test_clear_usage_logs_requires_admin(client, seeded):
    """member 与未登录一律拒绝（越权 403 由 main.py 审计处理器留痕）。"""
    from conftest import login

    login(client, seeded.emails["member"])
    r = client.delete("/api/v2/admin/usage/logs", headers=H)
    assert r.status_code == 403

    client.cookies.clear()  # 清掉 member 会话，验证真·未登录
    r = client.delete("/api/v2/admin/usage/logs", headers=H)
    assert r.status_code == 401
