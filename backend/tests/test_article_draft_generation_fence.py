"""重复投递/重新生成竞争条件的回归测试。"""
from datetime import datetime, timezone
import types

import pytest

from conftest import set_config
import app.services.article_gen_service as gen_mod
from app.models.article_draft import ArticleDraft
from app.services.article_gen_service import generate_article_for_draft

_LLM_OK = '{"title":"生成标题","digest":"生成摘要","content_md":"生成正文","alt_titles":[]}'
_USAGE = {"prompt_tokens": 100, "completion_tokens": 50}


def _llm_config(db):
    set_config(db, "llm_base_url", "https://llm.example.com/v1")
    set_config(db, "llm_api_key", "test-key")
    set_config(db, "llm_model", "test-model")


def _make_draft(db, source_type="event", source_id=1, status="generating"):
    session = db.factory()
    row = ArticleDraft(source_type=source_type, source_id=source_id, source_title="选题",
                       tier="brief", status=status)
    session.add(row)
    session.commit()
    draft_id = row.id
    session.close()
    return draft_id


def _draft_material(db):
    from app.models.article import Article
    from app.models.event import EventArticle, HotEvent

    session = db.factory()
    first = Article(title="报道甲", url="https://example.com/a", channel="科技", source="甲源",
                   content="甲内容" * 200, publish_time=datetime.now(timezone.utc))
    second = Article(title="报道乙", url="https://example.com/b", channel="科技", source="乙源",
                     content="乙内容" * 200, publish_time=datetime.now(timezone.utc))
    event = HotEvent(title="测试事件", summary="")
    session.add_all([first, second, event])
    session.flush()
    session.add_all([EventArticle(event_id=event.id, article_id=first.id),
                     EventArticle(event_id=event.id, article_id=second.id)])
    session.commit()
    event_id = event.id
    session.close()
    return event_id


@pytest.fixture()
def usage_calls(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.article_gen_service.record_usage", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture()
def seeded_content(db):
    return types.SimpleNamespace(ev_id=_draft_material(db))


def test_delayed_duplicate_does_not_overwrite_edited_draft(db):
    s = db.factory()
    row = ArticleDraft(source_type="event", source_id=1, source_title="选题", tier="brief",
                       gen_count=1, status="ready", title="已编辑标题", content_md="已编辑正文")
    s.add(row)
    s.commit()
    draft_id = row.id
    s.close()

    session = db.factory()
    try:
        ok, err = generate_article_for_draft(session, draft_id, generation=1)
        assert (ok, err) == (False, "generation superseded")
    finally:
        session.close()
    s = db.factory()
    assert s.get(ArticleDraft, draft_id).content_md == "已编辑正文"
    s.close()



def test_generation_stops_before_first_llm_call_when_disabled(db, seeded_content, usage_calls):
    _llm_config(db)
    set_config(db, "article_gen_enabled", "false")
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    calls = []
    original = gen_mod.llm_client.chat
    gen_mod.llm_client.chat = lambda *args, **kwargs: calls.append(args)
    try:
        ok, err = gen_mod.generate_article_for_draft(db.factory(), draft_id, generation=1)
    finally:
        gen_mod.llm_client.chat = original
    assert (ok, err) == (False, "article generation disabled")
    assert calls == [] and usage_calls == []
    from app.models.article_draft import ArticleDraft
    s = db.factory()
    row = s.get(ArticleDraft, draft_id)
    assert row.status == "failed" and "article_gen_enabled=false" in row.error
    s.close()


def test_generation_rechecks_disable_before_parse_retry(db, seeded_content, usage_calls):
    """第一次解析失败已产生用量，运维停用后不发出第二次调用。"""
    _llm_config(db)
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    calls = []
    original = gen_mod.llm_client.chat

    def fake_chat(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            set_config(db, "article_gen_enabled", "false")
            raise gen_mod.llm_client.LLMFormatError("无法解析", dict(_USAGE))
        raise AssertionError("停用后不得第二次调用")

    gen_mod.llm_client.chat = fake_chat
    try:
        ok, err = gen_mod.generate_article_for_draft(db.factory(), draft_id, generation=1)
    finally:
        gen_mod.llm_client.chat = original
    assert (ok, err) == (False, "article generation disabled")
    assert len(calls) == 1
    assert [item["status"] for item in usage_calls] == ["parse_fail"]
    assert usage_calls[0]["prompt_tokens"] == 100
    from app.models.article_draft import ArticleDraft
    s = db.factory()
    row = s.get(ArticleDraft, draft_id)
    assert row.status == "failed" and row.gen_count == 1
    s.close()


def test_generation_reads_new_committed_flag_despite_cached_identity(db, seeded_content, usage_calls):
    """长生命周期 Session 强引用旧 ORM 配置时，生成仍读已提交的新值。"""
    _llm_config(db)
    set_config(db, "article_gen_enabled", "true")
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    from app.models.setting import SystemSetting
    stale_session = db.factory()
    stale = stale_session.get(SystemSetting, "article_gen_enabled")
    assert stale.value == "true"
    writer = db.factory()
    writer.get(SystemSetting, "article_gen_enabled").value = "false"
    writer.commit()
    writer.close()
    calls = []
    original = gen_mod.llm_client.chat
    gen_mod.llm_client.chat = lambda *args, **kwargs: calls.append(1)
    try:
        result = gen_mod.generate_article_for_draft(stale_session, draft_id, generation=1)
    finally:
        gen_mod.llm_client.chat = original
        stale_session.close()
    assert result == (False, "article generation disabled")
    assert calls == [] and usage_calls == []


def test_inflight_llm_finishes_after_switch_is_disabled(db, seeded_content, usage_calls):
    """已发出的模型调用允许完成；停用只阻止后续请求。"""
    _llm_config(db)
    set_config(db, "article_gen_enabled", "true")
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    original = gen_mod.llm_client.chat

    def in_flight(*args, **kwargs):
        set_config(db, "article_gen_enabled", "false")
        return _LLM_OK, dict(_USAGE)

    gen_mod.llm_client.chat = in_flight
    try:
        ok, error = gen_mod.generate_article_for_draft(db.factory(), draft_id, generation=1)
    finally:
        gen_mod.llm_client.chat = original
    assert ok, error
    s = db.factory()
    assert s.get(ArticleDraft, draft_id).status == "ready"
    s.close()


def test_generation_resumes_after_reenable(db, seeded_content, usage_calls):
    _llm_config(db)
    set_config(db, "article_gen_enabled", "false")
    draft_id = _make_draft(db, source_type="event", source_id=seeded_content.ev_id, status="generating")
    session = db.factory()
    assert gen_mod.generate_article_for_draft(session, draft_id, generation=1)[0] is False
    session.close()
    set_config(db, "article_gen_enabled", "true")
    s = db.factory()
    from app.models.article_draft import ArticleDraft
    row = s.get(ArticleDraft, draft_id)
    row.status = "generating"
    s.commit()
    s.close()
    original = gen_mod.llm_client.chat
    gen_mod.llm_client.chat = lambda *args, **kwargs: (_LLM_OK, dict(_USAGE))
    try:
        ok, error = gen_mod.generate_article_for_draft(db.factory(), draft_id, generation=1)
    finally:
        gen_mod.llm_client.chat = original
    assert ok, error
    assert usage_calls[-1]["status"] == "success"


def test_old_generation_cannot_run_against_new_generation(db):
    s = db.factory()
    row = ArticleDraft(source_type="article", source_id=1, source_title="选题", tier="depth",
                       gen_count=2, status="generating")
    s.add(row)
    s.commit()
    draft_id = row.id
    s.close()
    s = db.factory()
    try:
        assert generate_article_for_draft(s, draft_id, generation=1) == (False, "generation superseded")
    finally:
        s.close()
    s = db.factory()
    assert s.get(ArticleDraft, draft_id).status == "generating"
    s.close()
