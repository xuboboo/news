"""网站资讯稿编辑简报与质量门回归。"""
from app.models.article_draft import ArticleDraft
from app.services.site_article_gen_service import (
    SITE_RESEARCH_SYSTEM_PROMPT,
    SITE_SYSTEM_PROMPT,
    _editorial_brief_prompt,
    _select_site_material,
)
from app.services.site_article_quality import (
    blocking_quality_issues,
    inspect_site_article,
    quality_feedback,
)


def _row(tier="brief"):
    return ArticleDraft(source_title="Ventoy 1.1.18 发布", tier=tier)


def _material():
    return [
        {
            "source": "IT之家", "title": "Ventoy 1.1.18 发布",
            "publish_time": "2026-10-09T15:38:00+08:00",
            "summary_short": "Ventoy 更新。",
            "content": "Ventoy 1.1.18 实验性支持 SteamOS 恢复镜像，并修复多个 Linux 启动问题。" * 30,
        },
        {
            "source": "快科技", "title": "Ventoy 新版支持 SteamOS",
            "publish_time": "2026-10-09T16:00:00+08:00",
            "summary_short": "新版支持 SteamOS。",
            "content": "Ventoy 新版加入 SteamOS 支持，Linux 挂载体验也有调整。" * 30,
        },
    ]


def _valid_data():
    return {
        "title": "Ventoy 1.1.18 支持 SteamOS 恢复镜像",
        "content_intro": "Ventoy 1.1.18 以实验性方式支持 SteamOS 恢复镜像，同时修复 Linux 启动与挂载问题。",
        "page_keywords": "Ventoy 1.1.18,SteamOS 恢复镜像,Linux 启动盘",
        "tags": "系统工具,软件更新",
        "content_md": (
            "Ventoy 1.1.18 已发布，新增 SteamOS 恢复镜像的实验性支持，并针对 Linux 和 Windows/WinPE 用户修复多项启动问题。"
            "对于需要制作系统启动盘的用户，这次更新主要改变了可承载的镜像范围和部分兼容性表现。\n\n"
            "## SteamOS 恢复镜像可直接放入 Ventoy 启动盘\n"
            "新版允许用户将 SteamOS 恢复镜像放入 Ventoy USB 存储盘并直接启动，但该功能仍处于实验阶段，部分设备或镜像可能存在兼容性限制。\n\n"
            "## Linux 挂载和启动兼容性同步调整\n"
            "新版通过重新挂载功能改善 Linux Live 环境下访问 ISO 分区的方式，并固定 ISO 分区设备名；同时修复 Fedora、Grml 和 Ubuntu Desktop Live 的部分问题。\n\n"
            "## Windows 与 WinPE 用户还会看到哪些变化\n"
            "面向 Windows 与 WinPE，新版修复 Secure Boot 版本检查失败，新增基于 Syslinux 的自定义 ISO 镜像支持，并提升与 LinuxConsole 的兼容性。"
        ),
    }


def test_research_prompt_requires_traceable_facts_and_search_intent():
    prompt = _editorial_brief_prompt(_row(), _material(), "事件总结线索")
    assert "编辑简报" in prompt
    assert "R1" in prompt and "source_ids" in prompt
    assert "读者实际会搜索" in SITE_RESEARCH_SYSTEM_PROMPT
    assert "single_source_update" in prompt
    assert "事件总结线索" in prompt


def test_select_material_removes_syndicated_duplicates_but_keeps_different_sources():
    material = _material()
    duplicate = dict(material[0], content=material[0]["content"] + "补充")
    selected = _select_site_material([material[0], duplicate, material[1]])
    assert len(selected) == 2
    assert selected[0]["content"].endswith("补充")


def test_quality_gate_accepts_specific_article():
    issues = inspect_site_article(_valid_data(), _material(), {"article_type": "release_update"}, "brief")
    assert blocking_quality_issues(issues) == []


def test_quality_gate_rejects_template_thin_and_single_source_overclaim():
    data = _valid_data()
    data["content_md"] = "导语。\n\n## 事件概述\n本文将带你了解这次变化。"
    data["content_intro"] = ""
    issues = inspect_site_article(
        data,
        [{"source": "媒体", "title": "单篇报道", "content": "事实"}],
        {"article_type": "single_source_update"},
        "brief",
    )
    codes = {item["code"] for item in issues}
    assert {"missing_intro", "too_thin", "generic_heading", "meta_talk"} <= codes
    assert blocking_quality_issues(issues)
    assert "编辑部质量复核" in quality_feedback(issues)


def test_quality_gate_flags_question_heading_overload_and_missing_data():
    data = _valid_data()
    data["content_md"] = data["content_md"].replace(
        "## Windows 与 WinPE 用户还会看到哪些变化",
        "## 怎么更新到新版？",
    )
    data["content_md"] += "\n\n## 更新后没收到短信怎么办？\n素材没有给出补充渠道，目前未知。"
    codes = {item["code"] for item in inspect_site_article(data, _material(), {"article_type": "release_update"}, "brief")}
    assert "heading_question_overload" in codes
    no_digit = dict(data)
    no_digit["content_md"] = "纯文字段落，没有任何数值信息。\n\n## 一节\n内容。\n\n## 另一节\n内容。"
    codes2 = {item["code"] for item in inspect_site_article(no_digit, _material(), {"article_type": "general_event"}, "brief")}
    assert "no_quotable_data" in codes2


def test_quality_gate_flags_judgement_deferral():
    data = _valid_data()
    data["content_md"] = data["content_md"].replace(
        "面向 Windows 与 WinPE",
        "读者可结合数据自行判断，面向 Windows 与 WinPE",
    )
    codes = {item["code"] for item in inspect_site_article(data, _material(), {"article_type": "release_update"}, "brief")}
    assert "judgment_deferral" in codes


def test_quality_gate_rejects_leaked_source_markers_and_ids():
    data = _valid_data()
    data["content_md"] += "\n\n参考来源：IT之家报道《Ventoy 1.1.18 发布》，编号 R1。"
    codes = {item["code"] for item in inspect_site_article(data, _material(), {"article_type": "release_update"}, "brief")}
    assert {"source_block_leak", "source_id_leak"} <= codes
    assert blocking_quality_issues(inspect_site_article(data, _material(), {}, "brief"))


def test_stray_source_marker_line_is_removed_before_footer_rebuild():
    from app.services.article_gen_service import _strip_stray_source_markers
    body = (
        "第一段事实。\n\n参考来源：IT之家报道《标题》，及 Linuxiac 博文。\n\n"
        "## 参考来源\n- 媒体丨《标题》"
    )
    cleaned = _strip_stray_source_markers(body)
    assert "参考来源：" not in cleaned
    assert "## 参考来源" in cleaned


def test_prompt_keeps_writer_focused_on_reader_value_not_keyword_stuffing():
    assert "搜索可理解性（SEO）" in SITE_SYSTEM_PROMPT
    assert "不能把‘换一种说法’当成原创" in SITE_RESEARCH_SYSTEM_PROMPT
    assert "关键词堆砌" in SITE_SYSTEM_PROMPT
    assert "第一手体验" in SITE_SYSTEM_PROMPT
