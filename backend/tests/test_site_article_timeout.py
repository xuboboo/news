"""网站稿慢模型/分阶段超时回归，模拟耗时，不等待、不调用供应商。"""
import json
import types

import pytest

import app.services.site_article_gen_service as service
from app.models.article_draft import ArticleDraft
from conftest import set_config


BRIEF = {
    "article_type": "release_update",
    "primary_search_intent": "Ventoy 1.1.18 有哪些变化",
    "reader_questions": ["哪些镜像可以启动", "Linux 有哪些修复"],
    "facts": [{"id": "F1", "claim": "新增 SteamOS 恢复镜像支持", "source_ids": ["R1"]}],
    "outline": [{"heading": "SteamOS 实验支持", "fact_ids": ["F1"]}],
}
MATERIAL = [{
    "source": "媒体", "title": "Ventoy 1.1.18 发布", "summary_short": "",
    "content": "Ventoy 1.1.18 新增 SteamOS 恢复镜像的实验支持，改善 Linux Live 环境的 ISO 分区挂载。"
               "新版修复 Fedora、Grml、Ubuntu 的启动或持久化问题，以及 Secure Boot 版本检查失败。",
}]
GOOD = {
    "title": "Ventoy 1.1.18 支持 SteamOS 恢复镜像",
    "content_intro": "Ventoy 1.1.18 以实验方式支持 SteamOS 恢复镜像，并改善 Linux ISO 分区挂载和启动兼容性。",
    "page_keywords": "Ventoy 1.1.18,SteamOS 恢复镜像,Linux 启动盘",
    "tags": "系统工具,软件更新", "alt_titles": [],
    "content_md": (
        "Ventoy 1.1.18 已发布，新增 SteamOS 恢复镜像的实验性支持，并针对 Linux 和 Windows/WinPE 用户修复多项启动问题。"
        "对于需要制作系统启动盘的用户，这次更新主要改变了可承载的镜像范围和部分兼容性表现。\n\n"
        "## SteamOS 恢复镜像可放入 Ventoy 启动盘\n"
        "新版允许用户将 SteamOS 恢复镜像放入 Ventoy USB 存储盘并直接启动，但该功能仍处于实验阶段，部分设备或镜像可能存在兼容性限制。\n\n"
        "## Linux 挂载和启动兼容性同步调整\n"
        "新版通过重新挂载功能改善 Linux Live 环境下访问 ISO 分区的方式，并固定 ISO 分区设备名；同时修复 Fedora、Grml 和 Ubuntu Desktop Live 的部分问题。\n\n"
        "## Windows 与 WinPE 用户还会看到哪些变化\n"
        "面向 Windows 与 WinPE，新版修复 Secure Boot 版本检查失败，新增基于 Syslinux 的自定义 ISO 镜像支持，并提升与 LinuxConsole 的兼容性。"
    ),
}


@pytest.fixture()
def generation(db, monkeypatch):
    for key, value in (
        ("llm_base_url", "https://llm.example.com/v1"), ("llm_api_key", "test-key"),
        ("llm_model", "slow-test"), ("llm_protocol", "openai"), ("article_gen_enabled", "true"),
    ):
        set_config(db, key, value)
    with db.factory() as session:
        row = ArticleDraft(channel="site", source_type="article", source_id=1,
                           source_title="Ventoy 1.1.18 发布", tier="brief", status="generating",
                           title="以前的标题", content_md="以前的正文")
        session.add(row)
        session.commit()
        draft_id = row.id
    monkeypatch.setattr(service, "_collect_material", lambda *args: (MATERIAL, ""))
    calls, logs = [], []
    clock = types.SimpleNamespace(now=1000.0)
    # 只覆写业务模块的时钟，避免影响 pytest/HTTP 桩及其他模块。
    monkeypatch.setattr(service, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(service, "record_usage", lambda **kwargs: logs.append(kwargs))
    return types.SimpleNamespace(id=draft_id, calls=calls, logs=logs, clock=clock)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_writer_can_finish_after_old_180_second_timeout(db, generation, monkeypatch, protocol):
    set_config(db, "llm_protocol", protocol)

    def chat(*args, **kwargs):
        generation.calls.append(kwargs)
        research = kwargs["system"] == service.SITE_RESEARCH_SYSTEM_PROMPT
        seconds = 110 if research else 190
        generation.clock.now += seconds
        if seconds > kwargs["timeout"].read:
            raise service.llm_client.LLMTimeoutError("timed out")
        return json.dumps(BRIEF if research else GOOD), {"prompt_tokens": 100, "completion_tokens": 50}

    monkeypatch.setattr(service.llm_client, "chat", chat)
    with db.factory() as session:
        assert service.generate_site_article_for_draft(session, generation.id, generation=1) == (True, "")
        row = session.get(ArticleDraft, generation.id, populate_existing=True)
        assert row.status == "ready" and row.revision == 2
        assert row.prompt_tokens == 300 and row.completion_tokens == 150
        assert "## 参考来源" not in row.content_md and "## 声明" not in row.content_md
    # 研究 + 写作候选1 + 写作候选2（best-of-2 择优，重写未触发）
    assert len(generation.calls) == 3
    assert generation.calls[0]["timeout"].read == 300
    assert [c["timeout"].read for c in generation.calls[1:]] == [600, 600]
    assert [c["max_tokens"] for c in generation.calls] == [8192, 12288, 12288]
    for call in generation.calls:
        assert call["timeout"].connect == 20 and call["timeout"].write == 30
        assert call["timeout"].pool == 20
    assert [log["latency_ms"] for log in generation.logs] == [110000, 190000, 190000]


def test_writer_read_timeout_fails_once_and_preserves_old_content(db, generation, monkeypatch):
    def chat(*args, **kwargs):
        generation.calls.append(kwargs)
        if len(generation.calls) == 1:
            generation.clock.now += 110
            return json.dumps(BRIEF), {"prompt_tokens": 100, "completion_tokens": 50}
        generation.clock.now += kwargs["timeout"].read
        raise service.llm_client.LLMTimeoutError("The read operation timed out", phase="read")

    monkeypatch.setattr(service.llm_client, "chat", chat)
    with db.factory() as session:
        ok, error = service.generate_site_article_for_draft(session, generation.id, generation=1)
        assert not ok and "600 秒" in error and "未自动重试" in error
        row = session.get(ArticleDraft, generation.id, populate_existing=True)
        assert row.status == "failed" and row.gen_count == 1 and row.revision == 2
        assert row.title == "以前的标题" and row.content_md == "以前的正文"
        assert "网站稿写作阶段失败" in row.error and "供应商用量无法确认" in row.error
    assert len(generation.calls) == 2  # 没有重复写作，也没有因质量检查再调用
    assert [r["status"] for r in generation.logs] == ["success", "api_fail"]
    assert generation.logs[-1]["latency_ms"] == 600000  # 单次耗时不是累计710秒
    assert generation.logs[-1]["error"].startswith("写作阶段：")


def test_research_timeout_does_not_fall_back_into_more_paid_calls(db, generation, monkeypatch):
    def chat(*args, **kwargs):
        generation.calls.append(kwargs)
        generation.clock.now += kwargs["timeout"].read
        raise service.llm_client.LLMTimeoutError("timed out")

    monkeypatch.setattr(service.llm_client, "chat", chat)
    with db.factory() as session:
        assert service.generate_site_article_for_draft(session, generation.id, generation=1)[0] is False
        row = session.get(ArticleDraft, generation.id, populate_existing=True)
        assert "编辑简报阶段" in row.error and "300 秒" in row.error
        assert row.content_md == "以前的正文"
    assert len(generation.calls) == 1 and generation.logs[0]["status"] == "api_fail"


def _request(monkeypatch, responses, enabled=lambda: True):
    calls, logs = [], []
    sequence = iter(responses)

    def chat(*args, **kwargs):
        calls.append({"prompt": args[4], **kwargs})
        result = next(sequence)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(service.llm_client, "chat", chat)
    outcome = service._request_json(
        "https://llm.example.test", "test-key", "slow-test", "openai", "test prompt", "system", 0.2,
        lambda status, error, **kwargs: logs.append({"status": status, "error": error, **kwargs}),
        enabled, stage="写作", read_timeout_seconds=600, max_tokens=12288,
    )
    return outcome, calls, logs


@pytest.mark.parametrize("phase,label", [("connect", "连接"), ("write", "发送请求"), ("pool", "获取连接")])
def test_phase_specific_timeout_message_no_automatic_retry(monkeypatch, phase, label):
    outcome, calls, logs = _request(monkeypatch, [service.llm_client.LLMTimeoutError("timeout", phase=phase)])
    assert outcome[0] is None and f"模型服务{label}超时" in outcome[2]
    assert len(calls) == 1 and len(logs) == 1 and logs[0]["status"] == "api_fail"


def test_parse_retry_totals_include_failed_attempt_and_feedback(monkeypatch):
    outcome, calls, logs = _request(monkeypatch, [
        ('{"title":', {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 2, "total_tokens": 15}),
        ('{"title":"ok"}', {"prompt_tokens": 20, "completion_tokens": 8, "cached_tokens": 4, "total_tokens": 28}),
    ])
    assert outcome == ({"title": "ok"}, {
        "prompt_tokens": 30, "completion_tokens": 13, "cached_tokens": 6, "total_tokens": 43,
    }, None)
    assert len(calls) == 2 and "无法解析" in calls[1]["prompt"]
    assert [row["status"] for row in logs] == ["parse_fail", "success"]
    assert all(row["latency_ms"] >= 0 for row in logs)


def test_disabled_before_parse_retry_keeps_known_usage(monkeypatch):
    flags = iter([True, False])
    outcome, calls, logs = _request(monkeypatch, [
        service.llm_client.LLMFormatError("bad format", {"prompt_tokens": 10, "completion_tokens": 5}),
    ], enabled=lambda: next(flags))
    assert outcome[0] is None and outcome[2] == "disabled"
    assert outcome[1]["prompt_tokens"] == 10 and outcome[1]["completion_tokens"] == 5
    assert len(calls) == 1 and [row["status"] for row in logs] == ["parse_fail"]


def test_api_error_remains_non_retryable(monkeypatch):
    outcome, calls, logs = _request(monkeypatch, [service.llm_client.LLMAPIError("HTTP 503")])
    assert outcome[2] == "HTTP 503" and len(calls) == 1
    assert logs[0]["status"] == "api_fail" and logs[0]["error"].startswith("写作阶段：")


def test_research_prompt_bounds_final_brief_not_factual_source(monkeypatch):
    row = ArticleDraft(source_title="测试选题", tier="brief")
    prompt = service._editorial_brief_prompt(row, MATERIAL, "")
    assert "最多 12 条不重复事实" in prompt
    assert "不输出思考" in prompt and "版本、日期、限制" in prompt
    assert MATERIAL[0]["content"] in prompt
