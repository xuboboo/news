"""文章草稿域回归测试：「选题 → AI 生成 → 编辑 → 复制发布」链路后端。

离线运行：python -m pytest tests/test_article_draft.py -q
覆盖：CSRF/登录门禁、参数校验、热配置开关 403、入队派发与审计、
LLM 成功/失败落库（api_fail / parse_fail 重试 / 字段缺失）、
重新生成幂等、编辑保存与删除。LLM 调用与 record_usage 均 monkeypatch，
不依赖真实网络与 postgres。
"""
import json
import types
from datetime import datetime, timezone

import pytest

import app.services.article_gen_service as gen_mod
import app.tasks.tasks as tasks_mod
from conftest import PW, login, set_config

H = {"X-Requested-With": "XMLHttpRequest"}

_LLM_OK = json.dumps({
    "title": "某厂发布新一代旗舰芯片 性能提升明显",
    "alt_titles": ["备选标题A", "备选标题B", "某厂 旗舰芯片 发布 性能"],
    "digest": "某厂发布新一代旗舰芯片，CPU 与 GPU 性能均有提升，首批搭载机型下月上市。",
    "content_md": (
        "某厂今日正式发布新一代旗舰芯片。\n\n"
        "## 1. 性能提升\n官方数据显示 CPU 提升 30%。\n\n"
        "[配图建议：芯片特写]\n\n"
        "## 2. 搭载机型\n首批机型下月上市。\n\n"
        "## 参考来源\n- IT之家丨《原标题A》\n- 快科技丨《原标题B》\n\n"
        "## 声明\n本文由 AI 基于公开报道辅助生成，内容经编辑整理。"
    ),
})
_USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "cached_tokens": 0, "total_tokens": 150}


def _llm_config(db):
    set_config(db, "llm_base_url", "https://llm.example.com/v1")
    set_config(db, "llm_api_key", "test-key")
    set_config(db, "llm_model", "glm-test")


@pytest.fixture()
def seeded_content(db):
    """事件 + 两篇成员文章（不同来源，保证综合叙事素材成立）。"""
    from app.models.article import Article
    from app.models.event import EventArticle, HotEvent

    s = db.factory()
    art1 = Article(title="原标题A", url="https://ithome.com/x/a", channel="软件",
                   source="IT之家", content="正文A" * 200,
                   publish_time=datetime.now(timezone.utc))
    art2 = Article(title="原标题B", url="https://mydrivers.com/x/b", channel="软件",
                   source="快科技", content="正文B" * 200,
                   publish_time=datetime.now(timezone.utc))
    ev = HotEvent(title="事件标题", summary="事件 AI 总结")
    s.add_all([art1, art2, ev])
    s.flush()
    s.add_all([
        EventArticle(event_id=ev.id, article_id=art1.id),
        EventArticle(event_id=ev.id, article_id=art2.id),
    ])
    s.commit()
    out = types.SimpleNamespace(ev_id=ev.id, art1_id=art1.id, art2_id=art2.id)
    s.close()
    return out


@pytest.fixture()
def fake_queue(monkeypatch):
    """拦截 article_gen_task.delay，记录入队调用（不真正进 Celery）。"""
    calls = []

    def fake_delay(draft_id, trigger="manual", generation=None):
        calls.append((draft_id, trigger, generation))
        return types.SimpleNamespace(id="task-fake")

    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", fake_delay)
    return calls


@pytest.fixture()
def usage_calls(monkeypatch):
    """拦截 record_usage（独立会话在离线测试连不上 postgres，改为捕获断言）。"""
    calls = []

    def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(gen_mod, "record_usage", fake_record)
    return calls


def _draft_revision(db, draft_id):
    from app.models.article_draft import ArticleDraft
    session = db.factory()
    revision = session.get(ArticleDraft, draft_id).revision
    session.close()
    return revision


def _make_draft(db, source_type="event", source_id=1, tier="brief", status="ready"):
    from app.models.article_draft import ArticleDraft

    s = db.factory()
    row = ArticleDraft(source_type=source_type, source_id=source_id,
                       source_title="事件标题", tier=tier, status=status)
    s.add(row)
    s.commit()
    out = row.id
    s.close()
    return out


# ———————————————————— API 门禁与校验 ————————————————————

def test_drafts_require_login(client):
    assert client.get("/api/v2/drafts").status_code == 401
    assert client.post("/api/v2/drafts", json={}, headers=H).status_code == 401


def test_create_draft_validation(client, db, seeded):
    login(client, seeded.emails["member"])
    # 变更类接口统一 CSRF 防线
    assert client.post("/api/v2/drafts", json={}).status_code == 403
    body = {"source_type": "event", "source_id": 1}
    assert client.post("/api/v2/drafts", json={**body, "source_type": "news"}, headers=H).status_code == 400
    assert client.post("/api/v2/drafts", json={**body, "tier": "long"}, headers=H).status_code == 400
    assert client.post("/api/v2/drafts", json={**body, "source_id": "abc"}, headers=H).status_code == 400
    assert client.post("/api/v2/drafts", json={**body, "source_id": 999}, headers=H).status_code == 404


@pytest.mark.parametrize("source_id", [1.9, True, 1.0])
def test_review_rejects_noninteger_source_ids_before_dispatch(client, db, seeded, seeded_content, fake_queue, source_id):
    login(client, seeded.emails["member"])
    response = client.post("/api/v2/drafts", json={"source_type": "article", "source_id": source_id}, headers=H)
    assert response.status_code == 400, response.text
    assert fake_queue == []


def test_review_strips_nested_source_sections():
    body = "正文\n\n## 参考来源\n### 来源补充\n- 假媒体丨《伪造报道》\n\n## 后续正文\n应保留内容"
    result = gen_mod._ensure_source_footer(body, [{"source": "可信媒体", "title": "真实标题"}])
    assert "假媒体" not in result
    assert "应保留内容" in result
    assert "真实标题" in result


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_review_preserves_fenced_heading_content(fence):
    body = f"正文\n\n{fence}text\n## 声明\n代码中的内容\n{fence}\n\n应保留的尾段"
    result = gen_mod._ensure_source_footer(body, [])
    assert body in result


def test_create_draft_disabled_403(client, db, seeded):
    set_config(db, "article_gen_enabled", "false")
    login(client, seeded.emails["member"])
    r = client.post("/api/v2/drafts", json={"source_type": "event", "source_id": 1}, headers=H)
    assert r.status_code == 403


def test_create_event_draft_dispatch_and_audit(client, db, seeded, seeded_content, fake_queue):
    login(client, seeded.emails["member"])
    r = client.post("/api/v2/drafts",
                    json={"source_type": "event", "source_id": seeded_content.ev_id, "tier": "depth"},
                    headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "generating"
    assert body["source_title"] == "事件标题"
    assert body["tier"] == "depth"
    # 单篇资讯源同样可建（不校验 AI 状态，摘要未完成也可用标题+正文生文）
    assert fake_queue == [(body["id"], "manual", 1)]
    # 审计留痕（哈希链域的动作清单新增）
    s = db.factory()
    from app.models.audit_log import AuditLog
    rows = s.query(AuditLog).filter(AuditLog.action == "draft.generated").all()
    s.close()
    assert any(r.detail.get("draft_id") == body["id"] for r in rows)


def test_create_article_source_draft(client, db, seeded, seeded_content, fake_queue):
    login(client, seeded.emails["member"])
    r = client.post("/api/v2/drafts",
                    json={"source_type": "article", "source_id": seeded_content.art1_id},
                    headers=H)
    assert r.status_code == 200
    assert r.json()["source_type"] == "article"


# ———————————————————— 生成服务（LLM 交互） ————————————————————

def test_generate_success(db, seeded_content, usage_calls):
    _llm_config(db)
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")

    def fake_chat(*args, **kwargs):
        return _LLM_OK, dict(_USAGE)

    import app.ai.llm_client as llm_mod
    orig_chat = llm_mod.chat
    gen_mod.llm_client.chat = fake_chat
    try:
        ok, err = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = orig_chat
    assert ok, err
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    s.close()
    assert row.status == "ready"
    assert row.title.startswith("某厂发布")
    assert "参考来源" in row.content_md
    assert "AI 基于公开报道辅助生成" not in row.content_md and "## 声明" not in row.content_md
    assert row.prompt_tokens == 100 and row.completion_tokens == 50
    assert sorted(row.source_refs, key=lambda ref: ref["source"]) == [
        {"source": "IT之家", "title": "原标题A", "url": "https://ithome.com/x/a"},
        {"source": "快科技", "title": "原标题B", "url": "https://mydrivers.com/x/b"},
    ]
    # 记账：task_type=article，event 源带 event_id，成功落 success
    assert usage_calls and usage_calls[-1]["task_type"] == "article"
    assert usage_calls[-1]["status"] == "success"
    assert usage_calls[-1]["event_id"] == seeded_content.ev_id


def test_generate_api_fail_marks_failed(db, seeded_content, usage_calls):
    _llm_config(db)
    draft_id = _make_draft(db, status="generating")

    def fake_chat(*args, **kwargs):
        raise gen_mod.llm_client.LLMAPIError("HTTP 500: boom")

    import app.ai.llm_client as llm_mod
    orig_chat = llm_mod.chat
    gen_mod.llm_client.chat = fake_chat
    try:
        ok, _ = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = orig_chat
    assert not ok
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    s.close()
    assert row.status == "failed"
    assert "LLM 调用失败" in row.error
    assert usage_calls[-1]["status"] == "api_fail"


def test_parse_failure_then_api_failure_does_not_reuse_usage(db, seeded_content, usage_calls):
    _llm_config(db)
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    calls = 0
    original = gen_mod.llm_client.chat

    def fail_first_then_api(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise gen_mod.llm_client.LLMFormatError("invalid JSON", dict(_USAGE))
        raise gen_mod.llm_client.LLMAPIError("provider unavailable")

    gen_mod.llm_client.chat = fail_first_then_api
    try:
        ok, _ = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = original
    assert not ok
    assert [entry["status"] for entry in usage_calls] == ["parse_fail", "api_fail"]
    assert usage_calls[0]["prompt_tokens"] == 100
    assert usage_calls[1].get("prompt_tokens", 0) == 0


def test_malformed_alt_titles_are_handled_and_usage_recorded(db, seeded_content, usage_calls):
    _llm_config(db)
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    malformed = json.dumps({"title": "标题", "digest": "摘要", "content_md": "正文", "alt_titles": 42})
    original = gen_mod.llm_client.chat
    gen_mod.llm_client.chat = lambda *args, **kwargs: (malformed, dict(_USAGE))
    try:
        ok, error = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = original
    assert ok, error
    assert usage_calls[-1]["status"] == "success"
    from app.models.article_draft import ArticleDraft
    session = db.factory()
    row = session.get(ArticleDraft, draft_id)
    assert row.status == "ready" and row.alt_titles == []
    session.close()


def test_generate_parse_fail_then_retry_success(db, seeded_content, usage_calls):
    """JSON 解析失败带反馈重试一次（与早报综述同款容错）。"""
    _llm_config(db)
    draft_id = _make_draft(db, status="generating")
    calls = {"n": 0}

    def fake_chat(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gen_mod.llm_client.LLMFormatError("无法解析", dict(_USAGE))
        assert "无法解析为 JSON" in args[4]  # prompt 已拼纠错反馈
        return _LLM_OK, dict(_USAGE)

    import app.ai.llm_client as llm_mod
    orig_chat = llm_mod.chat
    gen_mod.llm_client.chat = fake_chat
    try:
        ok, _ = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = orig_chat
    assert ok and calls["n"] == 2
    assert [c["status"] for c in usage_calls] == ["parse_fail", "success"]


def test_generate_missing_llm_config(db, usage_calls):
    draft_id = _make_draft(db, status="generating")
    ok, err = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    assert not ok and err == "llm not configured"
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    s.close()
    assert row.status == "failed" and "未配置 LLM" in row.error


def test_model_name_over_usage_log_limit_fails_before_spending(db, seeded_content, usage_calls):
    _llm_config(db)
    set_config(db, "article_gen_model", "m" * 65)
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    original = gen_mod.llm_client.chat
    gen_mod.llm_client.chat = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call model"))
    try:
        ok, error = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = original
    assert not ok and "64 字符上限" in error
    assert usage_calls == []


def test_source_footer_rebuilt_without_ai_disclosure(db, seeded_content, usage_calls):
    """即使模型输出声明，落库正文也只保留按真实素材重建的来源。"""
    _llm_config(db)
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    # 注意故意不含 参考来源 与 声明 两段
    injected = json.dumps({
        "title": "标题合规测试",
        "alt_titles": [],
        "digest": "摘要",
        "content_md": "正文第一段。\n\n## 1. 细节\n内容。",
    })

    def fake_chat(*args, **kwargs):
        return injected, dict(_USAGE)

    import app.ai.llm_client as llm_mod
    orig_chat = llm_mod.chat
    gen_mod.llm_client.chat = fake_chat
    try:
        ok, _ = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = orig_chat
    assert ok
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    s.close()
    assert "## 参考来源" in row.content_md
    assert "IT之家丨《原标题A》" in row.content_md
    assert "AI 基于公开报道辅助生成" not in row.content_md and "## 声明" not in row.content_md


def test_source_footer_kept_when_present(db, seeded_content, usage_calls):
    """LLM 输出来源段时由服务端按来源快照重建，且不保留声明段。"""
    _llm_config(db)
    draft_id = _make_draft(db, status="generating")

    def fake_chat(*args, **kwargs):
        return _LLM_OK, dict(_USAGE)

    import app.ai.llm_client as llm_mod
    orig_chat = llm_mod.chat
    gen_mod.llm_client.chat = fake_chat
    try:
        ok, _ = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = orig_chat
    assert ok
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    s.close()
    assert row.content_md.count("## 参考来源") == 1
    assert "AI 基于公开报道辅助生成" not in row.content_md and "## 声明" not in row.content_md


def test_event_material_limit_applies_after_filtering_empty_sources(db, seeded_content, monkeypatch):
    from datetime import timedelta
    from app.models.article import Article
    from app.models.event import EventArticle, HotEvent

    session = db.factory()
    event = HotEvent(title="独立选题", summary="")
    session.add(event)
    session.flush()
    event_id = event.id
    now = datetime.now(timezone.utc)
    usable = Article(title="较早的可用报道", url="https://test.local/usable", channel="科技", source="媒体A", content="事实素材", publish_time=now - timedelta(hours=1))
    empty = Article(title="最新但无内容", url="https://test.local/empty", channel="科技", source="媒体B", content="", publish_time=now)
    session.add_all([usable, empty])
    session.flush()
    session.add_all([EventArticle(event_id=event_id, article_id=usable.id),
                     EventArticle(event_id=event_id, article_id=empty.id)])
    session.commit()
    row = session.query(gen_mod.ArticleDraft).filter_by(source_id=event_id).first()
    if row is None:
        row = gen_mod.ArticleDraft(source_type="event", source_id=event_id, source_title="选题", tier="brief")
    monkeypatch.setattr(gen_mod.config_service, "get_effective", lambda _db, key: 1 if key == "article_gen_max_sources" else 4000)
    material, _ = gen_mod._collect_material(session, row)
    session.close()
    assert [item["title"] for item in material] == ["较早的可用报道"]


def test_generate_empty_material_fails_without_llm_call(db, seeded, usage_calls):
    """正文与摘要全空的源直接失败（素材为空），不烧 token 调 LLM。"""
    _llm_config(db)
    from datetime import datetime, timezone

    from app.models.article import Article
    from app.models.summary import ArticleSummary

    s = db.factory()
    art = Article(title="空文章", url="https://ithome.com/x/empty", channel="软件",
                  source="IT之家", content="",
                  publish_time=datetime.now(timezone.utc))
    s.add(art)
    s.flush()
    s.add(ArticleSummary(article_id=art.id, summary_short="   "))
    s.commit()
    art_id = art.id
    s.close()

    def fake_chat(*args, **kwargs):
        raise AssertionError("素材为空时不应调用 LLM")

    draft_id = _make_draft(db, source_type="article", source_id=art_id, status="generating")
    import app.ai.llm_client as llm_mod
    orig_chat = llm_mod.chat
    gen_mod.llm_client.chat = fake_chat
    try:
        ok, err = gen_mod.generate_article_for_draft(db.factory(), draft_id)
    finally:
        gen_mod.llm_client.chat = orig_chat
    assert not ok and err == "no material"
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    s.close()
    assert row.status == "failed" and "素材为空" in row.error
    assert usage_calls == []  # 未发生任何 LLM 计费


def test_source_footer_replaces_model_spoofed_sources():
    content = (
        "正文内容。\n\n## 参考来源\n- 假来源丨《不存在的报道》\n\n"
        "## 声明\n伪造的 AI 声明。"
    )
    result = gen_mod._ensure_source_footer(
        content, [{"source": "可信来源", "title": "真实标题"}]
    )
    assert "假来源" not in result
    assert "不存在的报道" not in result
    assert "- 可信来源丨《真实标题》" in result
    assert "AI 基于公开报道辅助生成" not in result and "伪造的 AI 声明" not in result


# ———————————————————— 编辑 / 重新生成 / 删除 ————————————————————

def test_dispatch_failure_marks_failed(client, db, seeded, seeded_content, monkeypatch):
    """入队失败（Redis 不可达等）必须落 failed，不能卡 generating 无重试入口。"""
    from app.models.article_draft import ArticleDraft

    def broken_delay(draft_id, trigger="manual", generation=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", broken_delay)
    login(client, seeded.emails["member"])
    r = client.post("/api/v2/drafts",
                    json={"source_type": "event", "source_id": seeded_content.ev_id},
                    headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "入队失败" in body["error"]
    s = db.factory()
    row = s.get(ArticleDraft, body["id"])
    s.close()
    assert row.status == "failed"


def test_dispatch_failure_consumes_budget_when_publish_result_is_ambiguous(client, db, seeded, seeded_content, monkeypatch):
    set_config(db, "article_gen_hourly_limit", "1")
    login(client, seeded.emails["member"])
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("redis down")))
    payload = {"source_type": "event", "source_id": seeded_content.ev_id}
    failed = client.post("/api/v2/drafts", json=payload, headers=H)
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *_args, **_kwargs: None)
    retried = client.post("/api/v2/drafts", json=payload, headers=H)
    assert retried.status_code == 429


def test_regenerate_stale_generating_allowed(client, db, seeded, fake_queue):
    """generating 超 15 分钟视为卡死，放行重新生成（worker 崩溃自救）。"""
    from datetime import datetime, timedelta, timezone

    from app.models.article_draft import ArticleDraft

    login(client, seeded.emails["member"])
    s = db.factory()
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    row = ArticleDraft(source_type="event", source_id=1, source_title="t", tier="brief",
                       status="generating", updated_at=stale_time, created_at=stale_time)
    s.add(row)
    s.commit()
    draft_id = row.id
    s.close()
    r = client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["gen_count"] == 2


def test_hourly_limit_blocks_and_zero_disables(client, db, seeded, seeded_content, fake_queue):
    """单用户每小时派发上限：超限 429；article_gen_hourly_limit=0 关闭限流。"""
    login(client, seeded.emails["member"])
    set_config(db, "article_gen_hourly_limit", "1")
    body = {"source_type": "event", "source_id": seeded_content.ev_id}
    assert client.post("/api/v2/drafts", json=body, headers=H).status_code == 200
    r = client.post("/api/v2/drafts", json=body, headers=H)
    assert r.status_code == 429
    assert "每小时生成上限" in r.json()["detail"]
    # 关闭限流后恢复放行
    set_config(db, "article_gen_hourly_limit", "0")
    assert client.post("/api/v2/drafts", json=body, headers=H).status_code == 200


def test_regenerate_old_draft_counts_toward_limit(client, db, seeded, seeded_content, fake_queue):
    """重新生成很久之前的旧稿同样占每小时限额（审计派发计数口径）。"""
    from datetime import datetime, timedelta, timezone

    from app.models.article_draft import ArticleDraft

    login(client, seeded.emails["member"])
    set_config(db, "article_gen_hourly_limit", "1")
    # 先创建一份草稿：限额 1 已被这次派发占满
    body = {"source_type": "event", "source_id": seeded_content.ev_id}
    assert client.post("/api/v2/drafts", json=body, headers=H).status_code == 200
    # 三天前的旧稿：旧口径（按近期所建草稿合计）下重新生成不占额度，新口径必须 429
    s = db.factory()
    old_time = datetime.now(timezone.utc) - timedelta(days=3)
    old_row = ArticleDraft(source_type="event", source_id=1, source_title="旧稿", tier="brief",
                           status="ready", created_at=old_time, updated_at=old_time,
                           created_by=seeded.ids["member"])
    s.add(old_row)
    s.commit()
    old_id = old_row.id
    s.close()
    r = client.post(f"/api/v2/drafts/{old_id}/regenerate", json={"revision": _draft_revision(db, old_id)}, headers=H)
    assert r.status_code == 429


def test_update_rejects_stale_revision_without_overwriting(client, db, seeded):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)
    revision = _draft_revision(db, draft_id)
    first = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": revision, "title": "甲的修改"}, headers=H)
    assert first.status_code == 200
    second = client.put(f"/api/v2/drafts/{draft_id}",
                        json={"revision": revision, "title": "乙的旧页面修改"}, headers=H)
    assert second.status_code == 409
    assert client.get(f"/api/v2/drafts/{draft_id}").json()["title"] == "甲的修改"


def test_regenerate_and_delete_reject_stale_revision(client, db, seeded, fake_queue):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)
    stale_revision = _draft_revision(db, draft_id)
    saved = client.put(
        f"/api/v2/drafts/{draft_id}",
        json={"revision": stale_revision, "title": "他人刚保存的标题"},
        headers=H,
    )
    assert saved.status_code == 200
    stale_regen = client.post(
        f"/api/v2/drafts/{draft_id}/regenerate",
        json={"revision": stale_revision},
        headers=H,
    )
    stale_delete = client.request("DELETE", f"/api/v2/drafts/{draft_id}", json={"revision": stale_revision}, headers=H)
    assert stale_regen.status_code == stale_delete.status_code == 409
    assert client.get(f"/api/v2/drafts/{draft_id}").json()["title"] == "他人刚保存的标题"
    assert fake_queue == []


def test_update_requires_revision(client, db, seeded):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)
    response = client.put(f"/api/v2/drafts/{draft_id}", json={"title": "没有版本"}, headers=H)
    assert response.status_code == 428


def test_update_manual_field_limits_and_null_semantics(client, db, seeded):
    """人工编辑按 Unicode codepoint 限长；null 兼容原有清空语义。"""
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)
    revision = _draft_revision(db, draft_id)
    title_30 = "😀" * 30
    digest_120 = "界" * 120
    ok = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": revision, "title": title_30, "digest": digest_120}, headers=H)
    assert ok.status_code == 200, ok.text
    revision_after_save = ok.json()["revision"]
    assert len(ok.json()["title"]) == 30
    assert len(ok.json()["digest"]) == 120
    for field, value in (("title", "😀" * 31), ("digest", "界" * 121)):
        bad = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": revision_after_save, field: value}, headers=H)
        assert bad.status_code == 400 and isinstance(bad.json()["detail"], str)
    for field, value in (("title", 9), ("digest", ["x"]), ("content_md", 12)):
        bad = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": revision_after_save, field: value}, headers=H)
        assert bad.status_code == 400 and isinstance(bad.json()["detail"], str)

    # 全字段校验先于写入：超长摘要不会提交同一 patch 中的合法标题。
    bad = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": revision_after_save, "title": "应保持不变", "digest": "x" * 121}, headers=H)
    assert bad.status_code == 400
    saved = client.get(f"/api/v2/drafts/{draft_id}").json()
    assert saved["title"] == title_30 and saved["digest"] == digest_120

    empty = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": revision_after_save, "title": "  ", "digest": None}, headers=H)
    assert empty.status_code == 200
    assert empty.json()["title"] == "" and empty.json()["digest"] == ""
    partial = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": empty.json()["revision"], "title": "部分更新"}, headers=H)
    assert partial.status_code == 200 and partial.json()["digest"] == ""


def test_regenerate_cas_rejects_stale_snapshot_without_dispatch(client, db, seeded, monkeypatch):
    """budget hook 期间另一会话推进版本，旧快照不得再次派发。"""
    from fastapi import HTTPException
    from app.models.article_draft import ArticleDraft
    from app.api.v2 import drafts as drafts_api

    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)
    calls = []
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *a, **k: calls.append((a, k)))
    original_budget = drafts_api._check_hourly_budget
    advanced = False

    def race_budget(session, user):
        nonlocal advanced
        original_budget(session, user)
        if not advanced:
            advanced = True
            other = db.factory()
            other.query(ArticleDraft).filter(ArticleDraft.id == draft_id).update({
                ArticleDraft.status: "generating", ArticleDraft.gen_count: 2,
            })
            other.commit()
            other.close()

    monkeypatch.setattr(drafts_api, "_check_hourly_budget", race_budget)
    response = client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H)
    assert response.status_code == 409
    assert calls == []


def test_dispatch_failure_does_not_overwrite_newer_generation(client, db, seeded, monkeypatch):
    """broker 已接收/抛错窗口里新代次就绪时，旧派发失败不能降级新稿。"""
    from app.models.article_draft import ArticleDraft

    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)

    def race_then_raise(*args, **kwargs):
        other = db.factory()
        other.query(ArticleDraft).filter(ArticleDraft.id == draft_id).update({
            ArticleDraft.status: "ready", ArticleDraft.gen_count: 3,
            ArticleDraft.title: "新一代已就绪", ArticleDraft.error: "",
        })
        other.commit()
        other.close()
        raise RuntimeError("late broker error")

    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", race_then_raise)
    response = client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["gen_count"] == 3
    s = db.factory()
    row = s.get(ArticleDraft, draft_id)
    assert row.title == "新一代已就绪" and row.status == "ready"
    s.close()


def test_dispatch_failure_after_concurrent_delete_does_not_resurrect(client, db, seeded, monkeypatch):
    from app.models.article_draft import ArticleDraft
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)

    def delete_then_raise(*_args, **_kwargs):
        other = db.factory()
        row = other.get(ArticleDraft, draft_id)
        other.delete(row)
        other.commit()
        other.close()
        raise RuntimeError("late broker error")

    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", delete_then_raise)
    response = client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H)
    assert response.status_code == 404
    s = db.factory()
    assert s.get(ArticleDraft, draft_id) is None
    s.close()


def test_update_cas_rejects_generation_started_during_save(client, db, seeded, monkeypatch):
    from app.models.article_draft import ArticleDraft
    from app.api.v2 import drafts as drafts_api

    login(client, seeded.emails["member"])
    draft_id = _make_draft(db)
    original_get = drafts_api._get_draft_or_404
    switched = False

    def switch_after_read(session, did):
        nonlocal switched
        row = original_get(session, did)
        if not switched:
            switched = True
            other = db.factory()
            other.query(ArticleDraft).filter(ArticleDraft.id == did).update({ArticleDraft.status: "generating"})
            other.commit()
            other.close()
        return row

    monkeypatch.setattr(drafts_api, "_get_draft_or_404", switch_after_read)
    response = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": _draft_revision(db, draft_id), "title": "不可覆盖"}, headers=H)
    assert response.status_code == 409
    s = db.factory()
    row = s.get(ArticleDraft, draft_id)
    assert row.status == "generating" and row.title == ""
    s.close()


def test_empty_ready_body_rejected_and_nonstandard_footer_spoof_removed(client, db, seeded, seeded_content):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id)
    empty = client.put(
        f"/api/v2/drafts/{draft_id}",
        json={"revision": _draft_revision(db, draft_id), "content_md": "   "},
        headers=H,
    )
    assert empty.status_code == 400

    from app.models.event import EventArticle
    session = db.factory()
    session.query(EventArticle).filter(EventArticle.event_id == seeded_content.ev_id).delete(synchronize_session=False)
    session.commit()
    session.close()
    malicious = client.put(
        f"/api/v2/drafts/{draft_id}",
        json={
            "revision": _draft_revision(db, draft_id),
            "content_md": "正文\n\n### 参考来源：伪造\n- 假媒体丨《假来源》\n\n## 声明：假声明\n伪造声明",
        },
        headers=H,
    )
    assert malicious.status_code == 200
    result = malicious.json()["content_md"]
    assert "假媒体" not in result and "假来源" not in result and "伪造声明" not in result
    assert "AI 基于公开报道辅助生成" not in result and "## 声明" not in result


def test_update_delete_roundtrip(client, db, seeded, seeded_content):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id)
    r = client.put(f"/api/v2/drafts/{draft_id}",
                   json={"revision": _draft_revision(db, draft_id), "title": "手工改的标题", "content_md": "## 1. 手工正文\n"},
                   headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "手工改的标题"
    assert r.json()["content_md"].startswith("## 1.")
    assert "AI 基于公开报道辅助生成" not in r.json()["content_md"] and "## 声明" not in r.json()["content_md"]
    assert "## 参考来源" in r.json()["content_md"]
    assert "原标题A" in r.json()["content_md"]
    from app.models.event import EventArticle
    session = db.factory()
    session.query(EventArticle).filter(EventArticle.event_id == seeded_content.ev_id).delete(synchronize_session=False)
    session.commit()
    session.close()
    updated_again = client.put(
        f"/api/v2/drafts/{draft_id}",
        json={"revision": r.json()["revision"], "content_md": "## 1. 再次编辑正文"},
        headers=H,
    )
    assert updated_again.status_code == 200
    assert "原标题A" in updated_again.json()["content_md"]
    assert "原标题B" in updated_again.json()["content_md"]
    detail = client.get(f"/api/v2/drafts/{draft_id}")
    assert detail.status_code == 200 and detail.json()["title"] == "手工改的标题"
    assert client.request("DELETE", f"/api/v2/drafts/{draft_id}", json={"revision": detail.json()["revision"]}, headers=H).status_code == 200
    assert client.get(f"/api/v2/drafts/{draft_id}").status_code == 404


def test_update_generating_conflict(client, db, seeded):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db, status="generating")
    assert client.put(f"/api/v2/drafts/{draft_id}", json={"title": "x"}, headers=H).status_code == 409


def test_regenerate_resets_and_dispatches(client, db, seeded, fake_queue):
    login(client, seeded.emails["member"])
    draft_id = _make_draft(db, status="ready")
    r = client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "generating"
    assert body["gen_count"] == 2
    assert fake_queue == [(draft_id, "manual", 2)]
    # 生成中不可重复触发
    assert client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H).status_code == 409


def test_drafts_are_shared_across_tenants_for_crud_and_regenerate(client, db, seeded, fake_queue):
    """草稿跨租户共享；任意已登录成员可读写，不以 created_by 做授权。"""
    from app.models.article_draft import ArticleDraft

    login(client, seeded.emails["member"])
    draft_id = _make_draft(db, status="ready")
    login(client, seeded.emails["member_b"])
    listed = client.get("/api/v2/drafts")
    assert listed.status_code == 200
    assert draft_id in [item["id"] for item in listed.json()["items"]]
    edited = client.put(f"/api/v2/drafts/{draft_id}", json={"revision": _draft_revision(db, draft_id), "title": "跨租户编辑"}, headers=H)
    assert edited.status_code == 200 and edited.json()["title"] == "跨租户编辑"
    regenerated = client.post(f"/api/v2/drafts/{draft_id}/regenerate", json={"revision": _draft_revision(db, draft_id)}, headers=H)
    assert regenerated.status_code == 200 and regenerated.json()["gen_count"] == 2
    deleted = client.request("DELETE", f"/api/v2/drafts/{draft_id}", json={"revision": regenerated.json()["revision"]}, headers=H)
    assert deleted.status_code == 200
    assert client.get(f"/api/v2/drafts/{draft_id}").status_code == 404
    assert fake_queue == [(draft_id, "manual", 2)]


def test_list_filters_and_pagination(client, db, seeded):
    login(client, seeded.emails["member"])
    ids = [_make_draft(db, status=s) for s in ("ready", "failed", "ready")]
    r = client.get("/api/v2/drafts", params={"status": "ready"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(item["status"] == "ready" for item in body["items"])
    assert client.get("/api/v2/drafts", params={"status": "bogus"}).status_code == 400
    one = client.get("/api/v2/drafts", params={"limit": 1, "offset": 0}).json()
    assert len(one["items"]) == 1 and one["total"] == 3
    cursor = one["items"][0]["id"]
    older = client.get("/api/v2/drafts", params={"limit": 1, "before_id": cursor}).json()
    assert older["total"] == 3
    assert len(older["items"]) == 1 and older["items"][0]["id"] < cursor
    # 列表不带正文，详情才带
    assert "content_md" not in one["items"][0]
