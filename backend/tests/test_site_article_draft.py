"""网站资讯稿回归测试：独立接口、SEO字段、网站生成提示词与公众号隔离。"""
import json
import types
from datetime import datetime, timezone

import pytest

import app.services.site_article_gen_service as site_gen
import app.tasks.tasks as tasks_mod
from conftest import login, set_config

H = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture()
def seeded_content(db):
    from app.models.article import Article
    from app.models.event import EventArticle, HotEvent
    session = db.factory()
    article = Article(title="芯片新闻", channel="科技", source="媒体", content="芯片发布，性能提升是官方公布的事实。",
                      url="https://example.com/chip", publish_time=datetime.now(timezone.utc))
    event = HotEvent(title="芯片发布事件", summary="芯片发布")
    session.add_all([article, event])
    session.flush()
    session.add(EventArticle(event_id=event.id, article_id=article.id))
    session.commit()
    result = types.SimpleNamespace(ev_id=event.id, art1_id=article.id)
    session.close()
    return result


def _llm_config(db):
    set_config(db, "llm_base_url", "https://llm.example.com/v1")
    set_config(db, "llm_api_key", "test-key")
    set_config(db, "llm_model", "glm-test")


def _make_site_draft(db, source_type="event", source_id=1, status="generating"):
    from app.models.article_draft import ArticleDraft
    session = db.factory()
    row = ArticleDraft(channel="site", source_type=source_type, source_id=source_id,
                       source_title="网站选题", tier="brief", status=status)
    session.add(row)
    session.commit()
    draft_id = row.id
    session.close()
    return draft_id


def test_site_create_dispatch_and_wechat_list_isolation(client, db, seeded, seeded_content, monkeypatch):
    calls = []
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: calls.append((args, kwargs)))
    login(client, seeded.emails["member"])
    response = client.post(
        "/api/v2/site-drafts",
        json={"source_type": "event", "source_id": seeded_content.ev_id, "tier": "depth"},
        headers=H,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel"] == "site"
    assert body["status"] == "generating"
    assert client.get(f"/api/v2/drafts/{body['id']}").status_code == 404
    assert client.request("DELETE", f"/api/v2/drafts/{body['id']}", json={"revision": 1}, headers=H).status_code == 404
    assert calls == [((body["id"],), {"trigger": "manual", "generation": 1})]
    assert body["id"] not in [item["id"] for item in client.get("/api/v2/drafts").json()["items"]]
    assert body["id"] in [item["id"] for item in client.get("/api/v2/site-drafts").json()["items"]]
    assert client.post("/api/v2/drafts", json={"channel": "site", "source_type": "event", "source_id": seeded_content.ev_id}, headers=H).status_code == 400


def test_site_rejects_noninteger_source_id_before_dispatch(client, db, seeded, seeded_content, monkeypatch):
    calls = []
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: calls.append(args))
    login(client, seeded.emails["member"])
    for value in (1.9, True, "1", 2**31):
        response = client.post("/api/v2/site-drafts", json={"source_type": "article", "source_id": value}, headers=H)
        assert response.status_code == 400
    assert calls == []


def test_site_generate_success_records_seo_fields(db, seeded_content, monkeypatch):
    _llm_config(db)
    draft_id = _make_site_draft(db, source_type="event", source_id=seeded_content.ev_id)
    usage_calls = []
    monkeypatch.setattr(site_gen, "record_usage", lambda **kwargs: usage_calls.append(kwargs))
    response_body = json.dumps({
        "title": "某厂发布新芯片：性能提升与首批机型信息",
        "alt_titles": ["新芯片发布详情", "某厂芯片性能提升", "芯片发布 首批机型"],
        "page_keywords": "新芯片、性能提升、首批机型",
        "tags": "芯片、科技硬件",
        "content_intro": "某厂发布新一代芯片，报道梳理性能提升、首批机型及行业影响。",
        "cover_guidance": "芯片实物与发布会现场的横版构图，主体清晰。",
        "software_id": 999,
        "content_md": "导语。\n\n## 性能信息\n报道中的事实。",
    })
    monkeypatch.setattr(site_gen.llm_client, "chat", lambda *args, **kwargs: (response_body, {"prompt_tokens": 120, "completion_tokens": 80}))
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert ok, error
    session = db.factory()
    from app.models.article_draft import ArticleDraft
    row = session.get(ArticleDraft, draft_id)
    assert row.channel == "site" and row.status == "ready"
    assert row.seo_keywords == "新芯片,性能提升,首批机型"
    assert row.site_tags == "芯片,科技硬件"
    assert row.software_id == 0  # AI 不能替编辑建立软件关联
    assert "## 参考来源" in row.content_md and "## 声明" in row.content_md
    assert "网站" in site_gen.SITE_SYSTEM_PROMPT
    assert "SEO" in site_gen.SITE_SYSTEM_PROMPT
    assert "供人工审核的网站科技资讯稿" in site_gen._site_prompt(row, [{"source": "媒体", "title": "标题", "summary_short": "", "content": "事实"}], "")
    session.close()
    assert usage_calls[-1]["task_type"] == "site_article"


def test_site_update_canonical_fields_and_revision(client, db, seeded, seeded_content, monkeypatch):
    login(client, seeded.emails["member"])
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: None)
    created = client.post("/api/v2/site-drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H).json()
    from app.models.article_draft import ArticleDraft
    session = db.factory()
    row = session.get(ArticleDraft, created["id"])
    row.status = "ready"
    row.title = "初始网站标题"
    row.content_md = "正文"
    session.commit()
    session.close()
    saved = client.put(
        f"/api/v2/site-drafts/{created['id']}",
        json={"revision": 1, "title": "网站标题", "seo_keywords": "关键词A、关键词B",
              "site_tags": "软件、教程", "cover_guidance": "横版软件界面图",
              "software_id": 12, "is_recommended": True, "content_md": "编辑后的正文"},
        headers=H,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["title"] == "网站标题"
    assert body["seo_keywords"] == "关键词A、关键词B"
    assert body["software_id"] == 12 and body["is_recommended"] is True
    assert "## 声明" in body["content_md"]
    stale = client.put(f"/api/v2/site-drafts/{created['id']}", json={"revision": 1, "title": "旧版本"}, headers=H)
    assert stale.status_code == 409


def test_site_and_wechat_share_hourly_budget(client, db, seeded, seeded_content, monkeypatch):
    set_config(db, "article_gen_hourly_limit", "1")
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *args, **kwargs: None)
    login(client, seeded.emails["member"])
    first = client.post("/api/v2/site-drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H)
    assert first.status_code == 200
    second = client.post("/api/v2/drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H)
    assert second.status_code == 429


def test_wechat_and_site_share_hourly_budget_in_reverse(client, db, seeded, seeded_content, monkeypatch):
    set_config(db, "article_gen_hourly_limit", "1")
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *args, **kwargs: None)
    login(client, seeded.emails["member"])
    first = client.post("/api/v2/drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H)
    assert first.status_code == 200
    second = client.post("/api/v2/site-drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H)
    assert second.status_code == 429


def test_site_save_rejects_invalid_flags_and_software_id(client, db, seeded, seeded_content, monkeypatch):
    login(client, seeded.emails["member"])
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: None)
    created = client.post("/api/v2/site-drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H).json()
    from app.models.article_draft import ArticleDraft
    session = db.factory()
    row = session.get(ArticleDraft, created["id"])
    row.status, row.title, row.digest, row.content_md = "ready", "标题", "简介", "正文"
    session.commit(); session.close()
    for patch in ({"software_id": -1}, {"software_id": True}, {"software_id": 1.5}, {"software_id": 2**31}, {"is_headline": "true"}, {"title": ""}, {"digest": ""}):
        response = client.put(f"/api/v2/site-drafts/{created['id']}", json={"revision": 1, **patch}, headers=H)
        assert response.status_code == 400, response.text
    detail = client.get(f"/api/v2/site-drafts/{created['id']}").json()
    assert detail["revision"] == 1 and detail["title"] == "标题" and detail["software_id"] == 0


def test_site_delete_revision_and_isolation(client, db, seeded, seeded_content, monkeypatch):
    from app.models.article_draft import ArticleDraft
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_mod.article_gen_task, "delay", lambda *args, **kwargs: None)
    login(client, seeded.emails["member"])
    original = client.post("/api/v2/drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H).json()
    website = client.post("/api/v2/site-drafts", json={"source_type": "event", "source_id": seeded_content.ev_id}, headers=H).json()
    assert client.get(f"/api/v2/site-drafts/{original['id']}").status_code == 404
    assert client.request("DELETE", f"/api/v2/site-drafts/{original['id']}", json={"revision": original["revision"]}, headers=H).status_code == 404
    invalid = client.request("DELETE", f"/api/v2/site-drafts/{website['id']}", json={"revision": website["revision"] + 1}, headers=H)
    assert invalid.status_code == 409
    assert client.get(f"/api/v2/site-drafts/{website['id']}").status_code == 200
    ok = client.request("DELETE", f"/api/v2/site-drafts/{website['id']}", json={"revision": website["revision"]}, headers=H)
    assert ok.status_code == 200
    session = db.factory()
    assert session.get(ArticleDraft, original["id"]) is not None
    session.close()


def test_site_queue_recovery_uses_site_task(db, monkeypatch):
    from app.models.article_draft import ArticleDraft
    from datetime import timedelta
    session = db.factory()
    row = ArticleDraft(channel="site", source_type="event", source_id=1, source_title="超时网站稿", tier="brief", status="generating",
                       created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                       updated_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    session.add(row); session.commit(); draft_id = row.id; session.close()
    monkeypatch.setattr(tasks_mod, "new_session", db.factory)
    dispatched = []
    monkeypatch.setattr(tasks_mod.site_article_gen_task, "delay", lambda *args, **kwargs: dispatched.append((args, kwargs)))
    tasks_mod.article_draft_queue_recovery_task()
    assert dispatched == [((draft_id,), {"trigger": "queue_recovery", "generation": 1})]


def test_site_llm_field_types_do_not_crash_after_token_usage(db, seeded_content, monkeypatch):
    _llm_config(db)
    draft_id = _make_site_draft(db, source_id=seeded_content.ev_id)
    usage_calls = []
    monkeypatch.setattr(site_gen, "record_usage", lambda **kwargs: usage_calls.append(kwargs))
    monkeypatch.setattr(site_gen.llm_client, "chat", lambda *args, **kwargs: (json.dumps({
        "title": "事实标题", "content_md": "正文事实", "alt_titles": 42,
        "page_keywords": ["关键词A", "关键词B"], "tags": {"unexpected": "value"},
        "content_intro": ["not a string"], "cover_guidance": True,
    }), {"prompt_tokens": 10, "completion_tokens": 5}))
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert ok, error
    assert usage_calls[-1]["status"] == "success" and usage_calls[-1]["task_type"] == "site_article"
    from app.models.article_draft import ArticleDraft
    session = db.factory(); row = session.get(ArticleDraft, draft_id)
    assert row.alt_titles == [] and row.digest and row.cover_guidance == ""
    session.close()


def test_site_missing_material_fails_without_model_call(db, seeded, monkeypatch):
    _llm_config(db)
    from app.models.article import Article
    session = db.factory()
    article = Article(title="无正文资讯", channel="科技", source="媒体", content="", url="https://example.com/empty",
                      publish_time=datetime.now(timezone.utc))
    session.add(article)
    session.commit()
    article_id = article.id
    session.close()
    draft_id = _make_site_draft(db, source_type="article", source_id=article_id)
    monkeypatch.setattr(site_gen.llm_client, "chat", lambda *args, **kwargs: pytest.fail("素材为空不应调用模型"))
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert not ok and error == "no material"
