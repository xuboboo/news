"""网站资讯稿回归测试：独立接口、SEO字段、网站生成提示词与公众号隔离。"""
import json
import types
from datetime import datetime, timezone

import pytest

import app.services.site_article_gen_service as site_gen
import app.tasks.tasks as tasks_mod
from conftest import login, set_config

H = {"X-Requested-With": "XMLHttpRequest"}

_RESEARCH_OK = json.dumps({
    "article_type": "general_event",
    "primary_search_intent": "芯片发布后有哪些已确认变化",
    "reader_questions": ["发生了什么", "谁会受到影响"],
    "news_angle": "围绕已确认变化和适用范围组织信息",
    "one_sentence_thesis": "报道确认芯片发布并披露性能变化。",
    "facts": [{"id": "F1", "claim": "芯片已发布", "source_ids": ["R1"], "certainty": "confirmed"}],
    "source_differences": [],
    "outline": [{"heading": "芯片发布信息", "purpose": "交代事实", "fact_ids": ["F1"]},
                 {"heading": "性能变化与适用范围", "purpose": "说明读者影响", "fact_ids": ["F1"]}],
    "must_not_claim": ["未提供的价格和测试数据"],
})


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
        "longtail_suggestions": [
            {"keyword": "新芯片支持哪些机型", "intent": "高", "form": "问答", "title": "新芯片支持哪些机型？首批名单与限制"},
            {"keyword": "新芯片怎么开启性能模式", "intent": "中", "form": "教程", "title": "新芯片性能模式开启路径"},
            {"垃圾": "条目"}, 42, "文本", {"keyword": "没有标题的条目", "intent": "高", "form": "问答", "title": ""},
        ],
        "software_id": 999,
        "content_md": (
            "芯片已经发布，官方公布了性能提升信息，读者可以先了解这次更新的核心变化。报道没有把所有规格一次列出，因此正文只保留已经明确出现的发布事实和性能表述。\n\n"
            "## 性能信息\n报道给出了芯片性能变化，现有材料能够确认发布事实，但没有提供完整测试方法。读者可以据此了解产品更新方向，不能把这段信息理解为独立实验室测试结果。\n\n"
            "## 适用范围\n当前报道主要面向关注芯片产品和首批机型的读者，具体适配范围仍应以正式信息为准。材料提到的应用对象和产品名称可以作为检索线索，但没有给出所有设备的兼容性清单。\n\n"
            "## 使用限制\n素材没有给出价格、第三方测试结果或所有设备的兼容性清单，正文不对这些内容作推断。后续如果厂商公布更多规格，编辑还需要根据新信息更新页面，而不是沿用未确认的说法。报道之外的价格和供货节奏，本稿不做任何补充。"
        ),
    })
    def fake_chat(*args, **kwargs):
        if "事实核查编辑" in kwargs.get("system", ""):
            return _RESEARCH_OK, {"prompt_tokens": 90, "completion_tokens": 70}
        return response_body, {"prompt_tokens": 120, "completion_tokens": 80}

    monkeypatch.setattr(site_gen.llm_client, "chat", fake_chat)
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert ok, error
    session = db.factory()
    from app.models.article_draft import ArticleDraft
    row = session.get(ArticleDraft, draft_id)
    assert row.channel == "site" and row.status == "ready"
    assert row.seo_keywords == "新芯片,性能提升,首批机型"
    assert row.site_tags == "芯片,科技硬件"
    # 长尾建议落库：非法条目（垃圾键/非dict/缺title）被清洗，只留两条合法项
    assert [item["keyword"] for item in row.longtail_suggestions] == ["新芯片支持哪些机型", "新芯片怎么开启性能模式"]
    assert row.software_id == 0  # AI 不能替编辑建立软件关联
    assert "## 参考来源" not in row.content_md and "## 声明" not in row.content_md
    assert "AI 基于公开报道辅助生成" not in row.content_md
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
    assert "## 声明" not in body["content_md"]
    assert "AI 基于公开报道辅助生成" not in body["content_md"]
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
    response_body = json.dumps({
        "title": "事实标题", "alt_titles": 42,
        "page_keywords": ["关键词A", "关键词B", "芯片发布"], "tags": "芯片,科技",
        "content_intro": "报道介绍芯片发布和性能变化，适用范围与未确认信息也需要按来源核对。", "cover_guidance": True,
        "content_md": (
            "芯片发布，性能提升是官方公布的事实，本文只保留报道可以核对的信息，读者可以先了解这次更新的核心变化，以及哪些说法目前只有单一来源。\n\n"
            "## 性能变化\n报道说明了产品发布和性能变化，但没有给出额外测试数据，因此本文不把官方表述改写成实验室结论。读者可以据此了解更新方向，不能据此推导完整性能排名，也不能把厂商口径当成第三方验证。\n\n"
            "## 读者范围\n当前材料面向关注芯片产品的读者，适用范围需要以正式信息为准。报道没有提供所有设备的兼容性清单，因此这里不扩大适用范围，首批机型之外设备的情况仍属未知。\n\n"
            "## 尚未确认的内容\n素材没有提供价格、兼容性清单或用户体验，正文不对这些内容作猜测。后续若有新规格或正式测试结果，应根据新来源更新页面，价格和供货时间也只在官方公布后补写，并提供给读者一个可以继续跟进的方向。"
        ),
    })

    def fake_chat(*args, **kwargs):
        if "事实核查编辑" in kwargs.get("system", ""):
            return _RESEARCH_OK, {"prompt_tokens": 10, "completion_tokens": 5}
        return response_body, {"prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr(site_gen.llm_client, "chat", fake_chat)
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert ok, error
    assert usage_calls[-1]["status"] == "success" and usage_calls[-1]["task_type"] == "site_article"
    from app.models.article_draft import ArticleDraft
    session = db.factory(); row = session.get(ArticleDraft, draft_id)
    assert row.alt_titles == [] and row.digest and row.cover_guidance == ""
    session.close()




def test_site_best_of_two_prefers_candidate_passing_gate(db, seeded_content, monkeypatch):
    _llm_config(db)
    draft_id = _make_site_draft(db, source_type="event", source_id=seeded_content.ev_id)
    good_body = (
        "芯片已经发布，官方公布了性能提升信息，读者可以先了解这次更新的核心变化。报道没有把所有规格一次列出，因此正文只保留已经明确出现的发布事实和性能表述。\n\n"
        "## 性能信息\n报道给出了芯片性能变化，现有材料能够确认发布事实，但没有提供完整测试方法。读者可以据此了解产品更新方向，不能把这段信息理解为独立实验室测试结果。\n\n"
        "## 适用范围\n当前报道主要面向关注芯片产品和首批机型的读者，具体适配范围仍应以正式信息为准。材料提到的应用对象和产品名称可以作为检索线索，但没有给出所有设备的兼容性清单。\n\n"
        "## 使用限制\n素材没有给出价格、第三方测试结果或所有设备的兼容性清单，正文不对这些内容作推断。后续如果厂商公布更多规格，编辑还需要根据新信息更新页面，而不是沿用未确认的说法。报道之外的价格和供货节奏，本稿不做任何补充。"
    )
    bad_body = "导语。\n\n## 事件概述\n本文将带你了解这次变化。"
    writing_calls = {"n": 0}

    def fake_chat(*args, **kwargs):
        if "事实核查编辑" in kwargs.get("system", ""):
            return _RESEARCH_OK, {"prompt_tokens": 10, "completion_tokens": 5}
        writing_calls["n"] += 1
        body = good_body if writing_calls["n"] == 1 else bad_body
        return json.dumps({"title": "候选标题", "alt_titles": [], "page_keywords": "芯片,性能", "tags": "芯片",
                           "content_intro": "内容简介", "cover_guidance": "", "software_id": 0,
                           "content_md": body}), {"prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr(site_gen.llm_client, "chat", fake_chat)
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert ok, error
    assert writing_calls["n"] == 2  # 两个候选都生成
    from app.models.article_draft import ArticleDraft
    session = db.factory(); row = session.get(ArticleDraft, draft_id)
    assert "性能信息" in row.content_md and "事件概述" not in row.content_md
    session.close()


def test_site_both_candidates_failing_triggers_single_rewrite(db, seeded_content, monkeypatch):
    _llm_config(db)
    draft_id = _make_site_draft(db, source_type="event", source_id=seeded_content.ev_id)
    good_body = (
        "芯片已经发布，官方公布了性能提升信息，读者可以先了解这次更新的核心变化。报道没有把所有规格一次列出，因此正文只保留已经明确出现的发布事实和性能表述。\n\n"
        "## 性能信息\n报道给出了芯片性能变化，现有材料能够确认发布事实，但没有提供完整测试方法。读者可以据此了解产品更新方向，不能把这段信息理解为独立实验室测试结果。\n\n"
        "## 适用范围\n当前报道主要面向关注芯片产品和首批机型的读者，具体适配范围仍应以正式信息为准。材料提到的应用对象和产品名称可以作为检索线索，但没有给出所有设备的兼容性清单。\n\n"
        "## 使用限制\n素材没有给出价格、第三方测试结果或所有设备的兼容性清单，正文不对这些内容作推断。后续如果厂商公布更多规格，编辑还需要根据新信息更新页面，而不是沿用未确认的说法。报道之外的价格和供货节奏，本稿不做任何补充。"
    )
    bad_body = "导语。\n\n## 事件概述\n本文将带你了解这次变化。"
    temps = []

    def fake_chat(*args, **kwargs):
        if "事实核查编辑" in kwargs.get("system", ""):
            return _RESEARCH_OK, {"prompt_tokens": 10, "completion_tokens": 5}
        temps.append(round(float(kwargs.get("temperature")), 2))
        body = bad_body if kwargs.get("temperature") != 0.35 else good_body
        return json.dumps({"title": "候选标题", "alt_titles": [], "page_keywords": "芯片,性能", "tags": "芯片",
                           "content_intro": "内容简介", "cover_guidance": "", "software_id": 0,
                           "content_md": body}), {"prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr(site_gen.llm_client, "chat", fake_chat)
    ok, error = site_gen.generate_site_article_for_draft(db.factory(), draft_id)
    assert ok, error
    assert temps == [0.45, 0.6, 0.35]  # 两候选都失败后，恰好定向重写一次
    from app.models.article_draft import ArticleDraft
    session = db.factory(); row = session.get(ArticleDraft, draft_id)
    assert "性能信息" in row.content_md
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
