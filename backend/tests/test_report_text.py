"""日报「行业变化」文案清洗与综述/markdown 推送单测：纯函数无 DB。"""
import base64
import hashlib
import hmac as hmac_mod

import pytest

from app.services.report_push import (
    _feishu_sign,
    build_payload,
    clip_markdown_bytes,
    push_targets,
    render_report_markdown,
)
from app.services.report_service import _clean_change_text, _compose_overview, _dedupe_texts


def test_strips_it_home_lede_prefix():
    raw = "IT之家 9 月 8 日消息，科技媒体 Windows Latest 今天（9 月 8 日）发布博文，报道称微软高管承诺听取反馈。"
    assert _clean_change_text(raw) == "微软高管承诺听取反馈"


def test_cuts_reporting_connector_within_first_sentence():
    raw = "科技媒体 Windows Central 昨日（9 月 7 日）发布博文，指出 Project Notch 将支持高刷屏切换。"
    assert _clean_change_text(raw).startswith("Project Notch")


def test_connector_in_second_sentence_does_not_hijack():
    raw = "苹果下调营收指引。库克表示需求疲软。补充" + "说明" * 60
    assert _clean_change_text(raw).startswith("苹果下调营收指引")


def test_strips_kj_xun_prefix():
    raw = "快科技 9 月 9 日讯，某厂发布新款处理器，性能提升明显。后续内容不属于首句。"
    out = _clean_change_text(raw)
    assert out == "某厂发布新款处理器，性能提升明显"


def test_keeps_text_without_lede():
    assert _clean_change_text("微软发布 Win11 预览版") == "微软发布 Win11 预览版"
    assert _clean_change_text("") == ""
    assert _clean_change_text(None) == ""


def test_first_sentence_preference():
    long = "苹果下调营收指引。库克表示需求疲软。" + "补充" * 60
    out = _clean_change_text(long)
    assert out == "苹果下调营收指引"


def test_short_first_sentence_falls_back_to_full_text():
    raw = "利好。某公司宣布全面转向 AI 优先战略，并且详细说明了后续三个季度的执行路线图"
    assert _clean_change_text(raw) == raw


def test_strips_ju_message_prefix():
    raw = "据韩媒 sedaily 消息，三星电子正加快其人形机器人原型机的研发。后续内容不属于首句。"
    assert _clean_change_text(raw) == "三星电子正加快其人形机器人原型机的研发"


def test_truncation_prefers_comma_boundary():
    body = "微软高管承诺将会基于用户反馈调整任务栏设计" + "，并且给出后续三个版本的改进路线" * 7
    out = _clean_change_text(body)
    assert len(out) <= 110
    assert out.endswith("…")
    assert out[:-1].endswith("线")  # 在最后一个逗号处截断而非半字硬切


def test_long_text_capped():
    raw = "这是一条没有任何句读的超长文本" + "，继续延续" * 30
    out = _clean_change_text(raw)
    assert len(out) <= 110
    assert out.endswith("…")


def test_dedupe_near_identical():
    items = [
        "科技媒体 Windows Central 发布博文称 Notchify 更新",
        "科技媒体 Windows Central 发布博文称 Notchify 更新，补充细节",
        "火狐 Firefox 157 将默认启用 Nova 新界面",
    ]
    out = _dedupe_texts(items)
    assert len(out) == 2
    assert out[0].startswith("科技媒体")
    assert out[1].startswith("火狐")


def test_overview_empty_when_no_articles():
    assert _compose_overview([], [], 0) == ""


def test_overview_lead_event_and_channels():
    events = [
        {"title": "微软发布 Win11 大版本更新", "articles_count": 3, "sources_count": 2},
        {"title": "DeepSeek 新模型内测", "articles_count": 1, "sources_count": 1},
    ]
    channels = [
        {"channel": "智能时代", "count": 26},
        {"channel": "软件之家", "count": 8},
    ]
    text = _compose_overview(events, channels, 42)
    assert text.startswith("今日最热「微软发布 Win11 大版本更新」，聚合 3 篇报道，2 家来源交叉报道。")
    assert "「DeepSeek 新模型内测」" in text
    assert "智能时代以 26 篇收录居首" in text
    assert "软件之家 8 篇次之" in text


def test_overview_single_event_no_rest():
    events = [{"title": "某事件", "articles_count": 1, "sources_count": 1}]
    text = _compose_overview(events, [], 5)
    assert text == "今日最热「某事件」。"


def test_render_markdown_sections():
    content = {
        "date": "2026-09-08",
        "issue_no": 147,
        "overview": "今日最热「A」。",
        "articles_total": 42,
        "top_events": [
            {"title": "A", "hot_score": 100, "articles_count": 3, "sources_count": 2}
        ],
        "industry_changes": ["变化一"],
        "quick_news": [{"title": "某文", "source": "IT之家"}],
        "channel_stats": [{"channel": "智能时代", "count": 26}],
        "generated_at": "2026-09-08T18:40:00+00:00",
    }
    md = render_report_markdown(content)
    assert "## 华军软件园 · 软件日报 2026-09-08（第 147 期）" in md
    assert "今日最热「A」。" in md
    assert "共收录 **42** 篇报道" in md
    assert "1. **A**（热度 100，3 篇/2 源）" in md
    assert "- 变化一" in md
    assert "- 某文（IT之家）" in md
    assert "- 智能时代：26 篇" in md
    assert "生成于" in md


def test_render_markdown_minimal():
    md = render_report_markdown({"date": "2026-09-01", "issue_no": 1})
    assert "软件日报 2026-09-01" in md


def test_feishu_payload_card_with_sign():
    p = build_payload("feishu", "# 标题", "s3cret")
    assert p["msg_type"] == "interactive"
    assert p["card"]["elements"][0] == {"tag": "markdown", "content": "# 标题"}
    # 飞书签名算法：key = "{timestamp}\n{secret}"（换行拼接），空消息 HMAC-SHA256 → base64
    key = p["timestamp"] + "\n" + "s3cret"
    expected = base64.b64encode(hmac_mod.new(key.encode(), digestmod=hashlib.sha256).digest()).decode()
    assert p["sign"] == expected


def test_feishu_payload_without_secret_has_no_sign():
    p = build_payload("feishu", "x")
    assert "sign" not in p and "timestamp" not in p


def test_wecom_payload_markdown():
    assert build_payload("wecom", "正文") == {"msgtype": "markdown", "markdown": {"content": "正文"}}


def test_feishu_sign_known_vector():
    # 与飞书官方文档算法一致的构造：仅验证 key 拼接与摘要确定性
    assert _feishu_sign("s", "123") == _feishu_sign("s", "123")
    assert _feishu_sign("s", "123") != _feishu_sign("s2", "123")


# ———— 企微 markdown 字节上限守卫（2026-09-10 审计） ————

def test_clip_markdown_bytes_keeps_short_content():
    assert clip_markdown_bytes("短内容") == "短内容"


def test_clip_markdown_bytes_truncates_at_line_boundary():
    md = "\n".join(f"- 第{i}条 " + "内容描述" * 30 for i in range(50))
    out = clip_markdown_bytes(md)
    assert len(out.encode("utf-8")) <= 4000
    assert out.endswith("（内容过长，已截断）")
    # 在行界截断：最后一个保留行完整（不以半个多字节字符或半行结尾）
    last_line = out.rstrip("…（内容过长，已截断）\n").split("\n")[-1]
    assert last_line.startswith("- 第")


def test_push_targets_skip_private_webhook(monkeypatch):
    """推送路径 SSRF 复核：env 直配的内网 webhook 被跳过（写入路径有校验，这里挡历史/直配值）。
    get_settings 带 lru_cache，本地 .env 可能为 true，须直接改缓存实例属性。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "allow_private_ai_urls", False)
    monkeypatch.setenv("REPORT_PUSH_ENABLED", "true")
    monkeypatch.setenv("REPORT_PUSH_WEBHOOK_URL", "https://192.168.1.9/cgi-bin/webhook/send")
    monkeypatch.delenv("REPORT_PUSH_FEISHU_WEBHOOK_URL", raising=False)
    assert push_targets(None) == []


def test_push_targets_public_wecom_and_feishu_env_secret(monkeypatch):
    """公网 URL 正常入列；飞书签名密钥支持 env 回退（db=None 路径此前读不到 env）。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "allow_private_ai_urls", False)
    monkeypatch.setenv("REPORT_PUSH_ENABLED", "true")
    monkeypatch.setenv("REPORT_PUSH_WEBHOOK_URL", "https://1.2.3.4/webhook")
    monkeypatch.setenv("REPORT_PUSH_FEISHU_WEBHOOK_URL", "https://5.6.7.8/hook")
    monkeypatch.setenv("REPORT_PUSH_FEISHU_SECRET", "s3cret")
    targets = push_targets(None)
    assert ("wecom", "https://1.2.3.4/webhook", "") in targets
    assert ("feishu", "https://5.6.7.8/hook", "s3cret") in targets


def test_push_targets_disabled_by_switch(monkeypatch):
    monkeypatch.setenv("REPORT_PUSH_ENABLED", "false")
    monkeypatch.setenv("REPORT_PUSH_WEBHOOK_URL", "https://1.2.3.4/webhook")
    assert push_targets(None) == []


# ———— /admin/report 入队前校验（2026-09-10 审计） ————

def test_trigger_report_rejects_bad_date_format():
    from fastapi import HTTPException

    from app.api.v2.routes import trigger_report

    with pytest.raises(HTTPException) as ei:
        trigger_report(date="not-a-date", request=None, db=None)
    assert ei.value.status_code == 400


def test_trigger_report_rejects_future_date():
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException

    from app.api.v2.routes import trigger_report

    future = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    with pytest.raises(HTTPException) as ei:
        trigger_report(date=future, request=None, db=None)
    assert ei.value.status_code == 400


# ———— _provider_ok 机器人响应形状校验（2026-09-11 二轮审计） ————

def _resp(payload):
    import httpx

    if payload is None:
        return httpx.Response(200, text="not json")
    return httpx.Response(200, json=payload)


def test_provider_ok_wecom():
    from app.services.report_push import _provider_ok

    assert _provider_ok("wecom", _resp({"errcode": 0, "errmsg": "ok"}))
    assert not _provider_ok("wecom", _resp({"errcode": 40008, "errmsg": "invalid media"}))
    # 200 但没有业务码键 = URL 误填到其他恰好 200 的服务，须判失败而非 ok
    assert not _provider_ok("wecom", _resp({"ok": True}))
    assert not _provider_ok("wecom", _resp(None))
    assert not _provider_ok("wecom", _resp(["list"]))


def test_provider_ok_feishu():
    from app.services.report_push import _provider_ok

    assert _provider_ok("feishu", _resp({"code": 0}))
    assert _provider_ok("feishu", _resp({"StatusCode": 0}))
    assert not _provider_ok("feishu", _resp({"code": 19021, "msg": "sign match fail"}))
    assert not _provider_ok("feishu", _resp({"hello": "world"}))


def test_overview_single_rest_phrasing():
    """仅剩 1 个后续热点时不再用「等 N 个」句式（等 1 个属语病）。"""
    events = [
        {"title": "A 事件标题", "articles_count": 1, "sources_count": 1},
        {"title": "B 事件标题", "articles_count": 1, "sources_count": 1},
    ]
    text = _compose_overview(events, [], 3)
    assert text.startswith("今日最热「A 事件标题」。")
    assert "此外，「B 事件标题」亦在持续发酵。" in text


# ———— 分通道渲染与 LLM 综述（2026-09-11 三轮：官方文档核对） ————

def _render_content():
    return {
        "date": "2026-09-10",
        "issue_no": 149,
        "overview": "今日综述内容。",
        "overview_source": "llm",
        "generated_at": "2026-09-10T00:30:00+00:00",
    }


def test_render_wecom_footer_keeps_font_tag():
    """企微 markdown 支持有限 HTML（font color=comment），保留。"""
    md = render_report_markdown(_render_content(), provider="wecom")
    assert '<font color="comment">生成于' in md
    assert "（AI 撰写）" in md  # 综述来源随推送透出


def test_render_feishu_footer_plain_text():
    """飞书卡片 markdown 不支持 HTML 标签（官方文档核对），<font> 会按原文显示，
    脚注须用纯文本 + 分割线。"""
    md = render_report_markdown(_render_content(), provider="feishu")
    assert "<font" not in md
    assert "---" in md
    assert "生成于" in md and "北京时间" in md


def _llm_overview(**kwargs):
    from app.services.report_service import _llm_overview

    return _llm_overview(("https://api.example.test/v1", "sk-x", "m1", "openai"), _render_content(), **kwargs)


def test_llm_overview_success(monkeypatch):
    from app.services import report_service

    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["payload"] = kw["json"]
        return type("R", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "choices": [{"message": {"content": '{"overview":"  今日综述：热点持续发酵。\\n数据齐全。"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        })()

    monkeypatch.setattr(report_service.llm_client.httpx, "post", fake_post)
    monkeypatch.setattr(report_service, "record_usage", lambda **kw: captured.setdefault("usage", kw))
    out = _llm_overview(trigger="auto")
    assert out == "今日综述：热点持续发酵。 数据齐全。"  # 空白归一
    assert captured["url"].endswith("/chat/completions")
    assert captured["payload"]["model"] == "m1"
    # 成功落账 task_type=report
    assert captured["usage"]["task_type"] == "report"
    assert captured["usage"]["status"] == "success"


def test_llm_overview_anthropic_protocol(monkeypatch):
    """llm_protocol=anthropic：请求走 /v1/messages + x-api-key，usage 按 input/output 映射落账。"""
    from app.services import report_service

    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["headers"] = kw["headers"]
        captured["payload"] = kw["json"]
        return type("R", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "content": [{"type": "text", "text": '{"overview":"Anthropic 协议综述。"}'}],
                "usage": {"input_tokens": 30, "output_tokens": 6},
            },
        })()

    monkeypatch.setattr(report_service.llm_client.httpx, "post", fake_post)
    monkeypatch.setattr(report_service, "record_usage", lambda **kw: captured.setdefault("usage", kw))
    from app.services.report_service import _llm_overview as _overview

    out = _overview(("https://api.example.test/anthropic", "sk-x", "m1", "anthropic"), _render_content())
    assert out == "Anthropic 协议综述。"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "sk-x"
    assert captured["headers"]["anthropic-version"]
    assert captured["payload"]["max_tokens"]  # anthropic 必填，适配层补齐
    assert captured["usage"]["status"] == "success"
    assert captured["usage"]["prompt_tokens"] == 30
    assert captured["usage"]["completion_tokens"] == 6


def test_llm_overview_api_fail_returns_none(monkeypatch):
    """连接失败 → api_fail 落账 → 返回 None（调用方回退模板）。"""
    import httpx as httpx_mod

    from app.services import report_service

    def boom(*a, **k):
        raise httpx_mod.ConnectError("connect timeout")

    monkeypatch.setattr(report_service.llm_client.httpx, "post", boom)
    captured = {}
    monkeypatch.setattr(report_service, "record_usage", lambda **kw: captured.setdefault("s", kw))
    assert _llm_overview() is None
    assert captured["s"]["status"] == "api_fail"
    assert captured["s"]["task_type"] == "report"


def test_llm_overview_invalid_url_classified_api_fail(monkeypatch):
    """InvalidURL 非 HTTPError 子类：漏接会被误记 parse_fail（token 未花却入「已花」账），
    必须归 api_fail（2026-09-12 审计修复回归）。"""
    import httpx as httpx_mod

    from app.services import report_service

    def boom(*a, **k):
        raise httpx_mod.InvalidURL("Invalid port: 'notaport'")

    monkeypatch.setattr(report_service.llm_client.httpx, "post", boom)
    captured = {}
    monkeypatch.setattr(report_service, "record_usage", lambda **kw: captured.setdefault("s", kw))
    assert _llm_overview() is None
    assert captured["s"]["status"] == "api_fail"
    assert captured["s"].get("total_tokens", 0) == 0  # api_fail 分支不带 token 键（=0）


def test_llm_overview_parse_fail_keeps_tokens(monkeypatch):
    """HTTP 200 但无 choices（结构异常）→ parse_fail 落账，已提取 usage 不丢。"""
    from app.services import report_service

    def fake_post(url, **kw):
        return type("R", (), {
            "status_code": 200,
            "raise_for_status": lambda self: None,
            "json": lambda self: {"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
        })()

    monkeypatch.setattr(report_service.llm_client.httpx, "post", fake_post)
    captured = {}
    monkeypatch.setattr(report_service, "record_usage", lambda **kw: captured.setdefault("s", kw))
    assert _llm_overview() is None
    assert captured["s"]["status"] == "parse_fail"
    assert captured["s"]["total_tokens"] == 10


def test_llm_overview_unconfigured_returns_none(monkeypatch):
    """未配置 LLM：不调用、不落账，直接 None。"""
    from app.services import report_service

    monkeypatch.setattr(report_service.llm_client.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发请求")))
    called = {}
    monkeypatch.setattr(report_service, "record_usage", lambda **kw: called.setdefault("s", kw))
    from app.services.report_service import _llm_overview

    assert _llm_overview(("", "", "", "openai"), _render_content()) is None
    assert not called
