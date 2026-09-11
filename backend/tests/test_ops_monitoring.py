"""运维监控域回归测试（第六轮审计修复的配套用例）。

离线运行：python -m pytest tests/test_ops_monitoring.py -q
覆盖：布尔/数值配置的后端强校验（拼写错误不再静默当 false）、
admin 路由 CSRF 防线覆盖、任务节奏心跳判停、告警消息格式与状态机转移、
/health?deep=1 深度探针结构。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models.task_run import TaskRun
from app.services import alert_service, infra_status
from app.utils.tz import bj_now

from conftest import login, set_config

# 与 test_users_auth 同款：变更请求必带的 CSRF 头
H = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture()
def super_client(client, seeded):
    """已登录平台超管的测试客户端（admin API 的常规访问路径）。"""
    login(client, seeded.emails["super"])
    return client


def _add_run(factory, name: str, minutes_ago: float, status: str = "success") -> None:
    s = factory()
    s.add(
        TaskRun(
            name=name,
            trigger="auto",
            status=status,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )
    )
    s.commit()
    s.close()


# ———————————————————— 运行时配置强校验 ————————————————————

def test_config_bool_spelling_error_rejected(super_client):
    """手滑拼错的布尔值必须 400，而不是静默当作 false 关闭功能。"""
    r = super_client.put(
        "/api/v2/admin/config", json={"stats_poll_enabled": "flase"}, headers=H
    )
    assert r.status_code == 400
    assert "true/false" in r.json()["detail"]


def test_config_bool_valid_values(super_client):
    r = super_client.put("/api/v2/admin/config", json={"stats_poll_enabled": "false"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["stats_poll_enabled"]["source"] == "db"
    assert body["config"]["stats_poll_enabled"]["value"] == "false"
    # 清除覆盖回落 .env 默认
    r = super_client.put("/api/v2/admin/config", json={"stats_poll_enabled": ""}, headers=H)
    assert r.status_code == 200
    assert r.json()["config"]["stats_poll_enabled"]["source"] == "env"


def test_config_numeric_range_enforced(super_client):
    """范围校验此前只在前端：直连接口可写入 0/越界等破坏值。"""
    assert (
        super_client.put("/api/v2/admin/config", json={"stats_poll_batch": "0"}, headers=H).status_code
        == 400
    )
    assert (
        super_client.put("/api/v2/admin/config", json={"sim_threshold": "1.5"}, headers=H).status_code
        == 400
    )
    assert (
        super_client.put("/api/v2/admin/config", json={"sim_threshold": "0.82"}, headers=H).status_code
        == 200
    )


def test_config_webhook_style_whitelist(super_client):
    assert (
        super_client.put(
            "/api/v2/admin/config", json={"alert_webhook_style": "slack"}, headers=H
        ).status_code
        == 400
    )
    assert (
        super_client.put(
            "/api/v2/admin/config", json={"alert_webhook_style": "dingtalk"}, headers=H
        ).status_code
        == 200
    )


def test_config_view_exposes_type(super_client):
    """type 字段供前端渲染开关/数字框/文本框。"""
    cfg = super_client.get("/api/v2/admin/config").json()["config"]
    assert cfg["cookie_secure"]["type"] == "bool"
    assert cfg["sim_threshold"]["type"] == "float"
    assert cfg["alert_webhook_url"]["type"] == "str"
    # alert_webhook_url 属敏感键：已配置时明文不出后端（未配置为空串，也无密可泄）
    assert cfg["alert_webhook_url"]["raw"] in (None, "")


# ———————————————————— CSRF 防线覆盖 ————————————————————

def test_admin_csrf_missing_header_403(super_client):
    """admin 路由此前不在 require_json_mutation 防线内。"""
    r = super_client.put("/api/v2/admin/config", json={"stats_poll_batch": "5"})
    assert r.status_code == 403


def test_admin_csrf_get_unaffected(super_client):
    assert super_client.get("/api/v2/admin/config").status_code == 200


# ———————————————————— 无效 admin token 的 401 与审计留痕 ————————————————————

def test_invalid_admin_token_401_and_audited(client, db):
    """错误 token 试探应急通道必须 401 并留审计痕（去重防刷表）。

    去重查询的 detail['path'].astext 是 PG 方言专有：加方言容错前，非 PG 方言
    （测试用 SQLite）下该路径 AttributeError 会把 401 变 500，且此路径零测试覆盖。
    """
    set_config(db, "admin_token", "right-token")
    r = client.get("/api/v2/admin/status", headers={"X-Admin-Token": "wrong-token"})
    assert r.status_code == 401

    s = db.factory()
    try:
        rows = s.execute(
            text("SELECT detail FROM audit_log WHERE action = 'auth.access_denied'")
        ).all()
    finally:
        s.close()
    # SQLite 把 JSON 存成文本，逐行解析后核对 reason
    import json as _json

    assert any(
        (_json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})).get("reason")
        == "invalid_admin_token"
        for row in rows
    )


def test_audit_actions_endpoint_matches_actions_set(super_client):
    """/audit-actions 下拉来源必须与 ACTIONS 清单一致——routes.py 的
    admin.test_report_push 埋点曾漏在清单外，UI 上筛不到该操作。"""
    from app.services.audit_service import ACTIONS

    r = super_client.get("/api/v2/admin/audit-actions")
    assert r.status_code == 200
    assert r.json()["items"] == sorted(ACTIONS)
    assert "admin.test_report_push" in r.json()["items"]


# ———————————————————— 任务节奏心跳 ————————————————————

def test_cadence_stale_event_flagged(db):
    _add_run(db.factory, "event", 90)  # 阈值 30 分钟
    warnings = infra_status.cadence_warnings(db.factory())
    assert [w["name"] for w in warnings] == ["event"]


def test_cadence_fresh_not_flagged(db):
    _add_run(db.factory, "event", 5)
    _add_run(db.factory, "report", 60)
    assert infra_status.cadence_warnings(db.factory()) == []


def test_cadence_never_ran_not_flagged(db):
    """全新部署从未运行过的任务不判停，避免起服满屏红灯。"""
    assert infra_status.cadence_warnings(db.factory()) == []


def test_cadence_stats_skippable_when_hot_disabled(db):
    _add_run(db.factory, "stats", 120)
    assert infra_status.cadence_warnings(db.factory(), skip={"stats"}) == []
    assert infra_status.cadence_warnings(db.factory()) != []


def test_cadence_crawler_threshold_follows_interval(db):
    _add_run(db.factory, "crawler", 100)
    # 间隔 30 分钟 → 阈值 90：100 分钟前 > 90 判停
    assert any(w["name"] == "crawler" for w in infra_status.cadence_warnings(db.factory(), 30))
    # 间隔拉长到 120 分钟 → 阈值 360：不判停
    assert not any(
        w["name"] == "crawler" for w in infra_status.cadence_warnings(db.factory(), 120)
    )


# ———————————————————— 告警服务 ————————————————————

def test_build_payload_styles():
    assert alert_service.build_payload("dingtalk", "hi") == {"msgtype": "text", "text": {"content": "hi"}}
    assert alert_service.build_payload("wecom", "hi") == {"msgtype": "text", "text": {"content": "hi"}}
    assert alert_service.build_payload("feishu", "hi") == {"msg_type": "text", "content": {"text": "hi"}}
    assert alert_service.build_payload("raw", "hi")["text"] == "hi"


def test_watch_disabled_without_url(db, monkeypatch):
    monkeypatch.setattr(alert_service, "_collect_problems", lambda s: ["Worker 全灭"])
    result = alert_service.watch(db.factory())
    assert result["notified"] is False
    assert "未配置" in result["reason"]


def test_watch_state_transitions(db, monkeypatch):
    """ok→bad 推故障；bad 持续期不重复轰炸；bad→ok 推恢复。"""
    from conftest import set_config

    set_config(db, "alert_webhook_url", "http://hooks.example/x")
    set_config(db, "alert_webhook_style", "wecom")

    state = {}
    pushes: list[tuple[str, str, str]] = []
    monkeypatch.setattr(alert_service, "redis_ping", lambda timeout=2.0: (True, 1))
    monkeypatch.setattr(alert_service, "_load_state", lambda: dict(state))
    monkeypatch.setattr(alert_service, "_save_state", lambda s: state.update(s) or True)
    monkeypatch.setattr(alert_service, "_push", lambda url, style, text: pushes.append((url, style, text)))

    monkeypatch.setattr(alert_service, "_collect_problems", lambda s: ["无在线 Celery Worker"])
    r1 = alert_service.watch(db.factory())
    assert r1["notified"] is True and len(pushes) == 1
    assert "【故障】" in pushes[0][2]

    r2 = alert_service.watch(db.factory())  # 持续坏：30 分钟内不重发
    assert r2["notified"] is False and len(pushes) == 1

    monkeypatch.setattr(alert_service, "_collect_problems", lambda s: [])
    r3 = alert_service.watch(db.factory())
    assert r3["notified"] is True and len(pushes) == 2
    assert "【恢复】" in pushes[1][2]


def test_watch_redis_down_no_spam(db, monkeypatch):
    """Redis 不可用时状态机停用：只记日志不推送，避免每 5 分钟重复轰炸。"""
    monkeypatch.setattr(alert_service, "redis_ping", lambda timeout=2.0: (False, None))
    pushed = []
    monkeypatch.setattr(alert_service, "_push", lambda url, style, text: pushed.append(text))
    monkeypatch.setattr(alert_service, "_collect_problems", lambda s: ["Redis 无法连接"])
    r = alert_service.watch(db.factory())
    assert r["notified"] is False and pushed == []


def test_watch_private_webhook_blocked(db, monkeypatch):
    """发送前 SSRF 复核（与 report_push 同口径）：面板写入时已校验，但 env 直配/
    旁路写表的私网地址不经校验——告警每 5 分钟向该地址 POST，必须拦截。"""
    import types

    from app.utils import url_guard
    from conftest import set_config

    # 测试宿主机 .env 可能开了 ALLOW_PRIVATE_AI_URLS（内网 embedding 部署），
    # 复核须在「豁免关闭」的前提下验证拦截行为
    monkeypatch.setattr(
        url_guard, "get_settings", lambda: types.SimpleNamespace(allow_private_ai_urls=False)
    )
    set_config(db, "alert_webhook_url", "http://127.0.0.1:9999/hook")
    pushed = []
    monkeypatch.setattr(alert_service, "redis_ping", lambda timeout=2.0: (True, 1))
    monkeypatch.setattr(alert_service, "_push", lambda url, style, text: pushed.append(text))
    monkeypatch.setattr(alert_service, "_collect_problems", lambda s: ["Worker 全灭"])
    r = alert_service.watch(db.factory())
    assert r["notified"] is False and pushed == []
    assert "非公网" in r["reason"]


# ———————————————————— 深度健康探针 ————————————————————

def test_health_light_and_deep(client, monkeypatch):
    # 测试库不含 article 表（pgvector）：轻探针走 db 失败路径，恰好验证 degraded 行为
    light = client.get("/api/v2/health").json()
    assert light["status"] == "degraded" and light["db"] is False

    # deep 结果有 15s 模块级缓存：先清掉，确保本用例的 monkeypatch 生效
    from app.api.v2 import public as public_api

    public_api._DEEP_CACHE["t"] = 0.0
    public_api._DEEP_CACHE["data"] = None
    monkeypatch.setattr(infra_status, "redis_ping", lambda timeout=2.0: (True, 3))
    monkeypatch.setattr(infra_status, "celery_workers_ping", lambda timeout=4.0: (["celery@a"], None))
    deep = client.get("/api/v2/health?deep=true").json()
    assert deep["status"] == "degraded"
    assert deep["failed"] == ["db"]
    assert deep["checks"]["redis"] is True
    assert deep["checks"]["celery"] is True
    assert deep["checks"]["crawler"] is None  # 从未运行过：不判停


# ———————————————————— AI 日成本预算告警（成本看板域 × 告警域） ————————————————————

def test_budget_alert_triggers_and_clears(db, monkeypatch):
    """ai_daily_cost_budget > 0 且今日成本超限 → 进 problems；预算 0 = 关闭。"""
    from app.models.usage import AiUsageLog
    from conftest import set_config

    # 基础设施项全部正常，隔离出预算检查的信号
    monkeypatch.setattr(infra_status, "redis_ping", lambda timeout=2.0: (True, 1))
    monkeypatch.setattr(infra_status, "celery_workers_ping", lambda timeout=4.0: (["celery@a"], None))
    monkeypatch.setattr(alert_service, "cadence_warnings", lambda db, interval, skip=None: [])

    s = db.factory()
    s.add(AiUsageLog(task_type="summary", trigger="auto", model="llm-a",
                     status="success", cost=5.0,
                     created_at=bj_now()))
    s.commit()
    s.close()

    # 预算 0（默认，env 未配置）= 关闭，不产生预算问题
    problems = alert_service._collect_problems(db.factory())
    assert all("预算" not in p for p in problems)

    set_config(db, "ai_daily_cost_budget", "2")
    problems = alert_service._collect_problems(db.factory())
    assert any("AI 日成本超预算" in p and "250%" in p for p in problems)

    # 未超限的预算不误报
    set_config(db, "ai_daily_cost_budget", "100")
    problems = alert_service._collect_problems(db.factory())
    assert all("预算" not in p for p in problems)
