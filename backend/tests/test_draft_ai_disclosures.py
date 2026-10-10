"""移除自动 AI 声明的回归：提示词、历史详情、尾注清理与存量迁移。"""
import pytest

from app.models.article_draft import ArticleDraft
from app.services.article_gen_service import (
    _SYSTEM_PROMPT,
    _build_prompt,
    _ensure_source_footer,
    _strip_generated_footer_sections,
)
from app.services.site_article_gen_service import SITE_SYSTEM_PROMPT, _site_prompt
from conftest import login
from scripts.remove_draft_ai_disclosures import clean_draft_disclosures

OLD_DISCLOSURE = "本文由 AI 基于公开报道辅助生成，内容经编辑整理。"
REFS = [{"source": "可信媒体", "title": "事实报道"}]
SOURCES = "## 参考来源\n- 可信媒体丨《事实报道》"


@pytest.mark.parametrize("heading", [
    "## 声明", "### 声明：旧尾注", "## AI 声明", "## AI生成声明",
    "## AI 辅助生成声明", "## AIGC 生成说明", "## 人工智能生成声明",
])
def test_removes_old_declaration_headings_and_keeps_later_body(heading):
    content = f"正文\n\n{heading}\n{OLD_DISCLOSURE}\n\n## 后续正文\n应保留的事实"
    result = _ensure_source_footer(content, REFS)
    assert OLD_DISCLOSURE not in result and heading not in result
    assert "应保留的事实" in result and SOURCES in result
    assert _ensure_source_footer(result, REFS) == result


@pytest.mark.parametrize("line", [
    OLD_DISCLOSURE, f"**{OLD_DISCLOSURE}**", "本文由AI辅助生成。", "本稿由人工智能生成。",
    "AI 辅助生成声明：本文由 AI 辅助整理撰写，内容基于上述公开报道中的事实信息组织而成，须经人工审核后方可发布。",
    "本文由 AI 辅助整理撰写，内容基于公开报道。",
])
def test_removes_standalone_disclosure_without_adding_any(line):
    result = _ensure_source_footer(f"正文\n\n{line}", [])
    assert result == "正文"
    assert _ensure_source_footer("", []) == ""


def test_history_cleanup_keeps_source_footer_and_real_ai_news():
    original = f"AI 产品发布，这是报道中的事实。\n\n{SOURCES}\n\n## 声明\n{OLD_DISCLOSURE}"
    expected = f"AI 产品发布，这是报道中的事实。\n\n{SOURCES}"
    assert _strip_generated_footer_sections(original, strip_sources=False) == expected
    # 无声明的文章不顺带改变换行和尾部空白。
    untouched = "正常正文\r\n\r\n## 官方声明\r\n厂商公布的事实。  \r\n"
    assert _strip_generated_footer_sections(untouched, strip_sources=False) == untouched


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_history_cleanup_preserves_fenced_examples(fence):
    original = f"示例代码：\n\n{fence}text\n## 声明\n{OLD_DISCLOSURE}\n{fence}\n\n{SOURCES}"
    assert _strip_generated_footer_sections(original, strip_sources=False) == original


@pytest.mark.parametrize("tier", ["brief", "depth"])
def test_both_prompts_no_longer_request_a_disclosure(tier):
    row = ArticleDraft(source_title="测试选题", tier=tier)
    material = [{"source": "可信媒体", "title": "事实报道", "content": "官方发布新功能。", "summary_short": ""}]
    for prompt in (_SYSTEM_PROMPT, SITE_SYSTEM_PROMPT, _build_prompt(row, material, ""), _site_prompt(row, material, "")):
        assert "声明" not in prompt and OLD_DISCLOSURE not in prompt
    assert "参考来源" in _build_prompt(row, material, "")
    assert "两个固定小节" not in _build_prompt(row, material, "")
    assert "参考来源" in _site_prompt(row, material, "")


@pytest.mark.parametrize("channel,route", [("wechat", "drafts"), ("site", "site-drafts")])
def test_old_draft_detail_and_save_strip_disclosure(client, db, seeded, channel, route):
    original = f"原正文\n\n{SOURCES}\n\n## 声明\n{OLD_DISCLOSURE}"
    with db.factory() as session:
        row = ArticleDraft(channel=channel, source_type="article", source_id=999,
                           source_title="旧选题", status="ready", title="旧标题", digest="旧摘要",
                           content_md=original, source_refs=REFS)
        session.add(row)
        session.commit()
        draft_id = row.id
    login(client, seeded.emails["member"])
    detail = client.get(f"/api/v2/{route}/{draft_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["content_md"] == f"原正文\n\n{SOURCES}"
    assert body["source_refs"] == REFS and body["revision"] == 1
    # 详情过滤为只读；清理数据库另由显式迁移执行。
    with db.factory() as session:
        assert session.get(ArticleDraft, draft_id).content_md == original
    response = client.put(f"/api/v2/{route}/{draft_id}", headers={"X-Requested-With": "XMLHttpRequest"},
                          json={"revision": 1, "content_md": f"编辑后正文\n\n## AI 声明\n{OLD_DISCLOSURE}"})
    assert response.status_code == 200, response.text
    assert response.json()["content_md"] == f"编辑后正文\n\n{SOURCES}"
    with db.factory() as session:
        stored = session.get(ArticleDraft, draft_id)
        assert OLD_DISCLOSURE not in stored.content_md and "## AI 声明" not in stored.content_md
        assert stored.revision == 2 and stored.source_refs == REFS


@pytest.mark.parametrize("channel", ["wechat", "site"])
def test_explicit_cleanup_is_previewable_idempotent_and_preserves_records(db, channel):
    original = f"历史正文\n\n{SOURCES}\n\n## 声明\n{OLD_DISCLOSURE}"
    with db.factory() as session:
        row = ArticleDraft(channel=channel, source_type="article", source_id=99,
                           source_title="历史选题", status="ready", title="历史标题", digest="历史摘要",
                           content_md=original, source_refs=REFS, model="original-model",
                           prompt_tokens=12, completion_tokens=34, gen_count=2, revision=5)
        session.add(row)
        session.commit()
        draft_id = row.id
        preview = clean_draft_disclosures(session)
        assert preview["changed_ids"] == [draft_id]
        assert session.get(ArticleDraft, draft_id).content_md == original
        applied = clean_draft_disclosures(session, apply=True)
        assert applied["changed_ids"] == [draft_id] and not applied["skipped_ids"]
        stored = session.get(ArticleDraft, draft_id, populate_existing=True)
        assert stored.content_md == f"历史正文\n\n{SOURCES}" and stored.revision == 6
        assert stored.source_refs == REFS and stored.gen_count == 2
        assert stored.title == "历史标题" and stored.digest == "历史摘要"
        assert stored.model == "original-model" and stored.prompt_tokens == 12 and stored.completion_tokens == 34
        assert clean_draft_disclosures(session, apply=True)["changed_ids"] == []
        assert session.get(ArticleDraft, draft_id, populate_existing=True).revision == 6


def test_cleanup_skips_generating_drafts(db):
    with db.factory() as session:
        row = ArticleDraft(channel="site", source_type="article", source_id=99, status="generating",
                           content_md=f"旧正文\n\n## 声明\n{OLD_DISCLOSURE}")
        session.add(row)
        session.commit()
        draft_id = row.id
        result = clean_draft_disclosures(session, apply=True)
        assert result["changed_ids"] == [] and result["skipped_ids"] == [draft_id]
        stored = session.get(ArticleDraft, draft_id, populate_existing=True)
        assert OLD_DISCLOSURE in stored.content_md and stored.revision == 1
