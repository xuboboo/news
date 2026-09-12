"""LLM 双协议适配层回归测试（llm_protocol = openai / anthropic）。

锁定三件事：
1. 协议差异收敛在 llm_client——请求形态（端点/认证头/max_tokens）与响应解析
   （choices[0].message.content vs content[].text）各按各的协议走；
2. usage 归一化——anthropic 的 input/output/cache_* 映射为 OpenAI 四项口径，
   与落账、成本看板同一本账；parse_fail 时已提取的 token 不丢账；
3. 配置层——llm_protocol 仅接受 openai/anthropic，脏值回落 openai 不抛错。

离线运行：httpx.post 整体替换为桩响应，不触网。
"""
import httpx
import pytest

from app.ai import llm_client
from app.services import config_service


# ———————————————————— normalize_protocol ————————————————————

def test_normalize_protocol_accepts_canonical_and_blank():
    assert llm_client.normalize_protocol("openai") == "openai"
    assert llm_client.normalize_protocol("anthropic") == "anthropic"
    assert llm_client.normalize_protocol("") == "openai"  # 空值 = 默认兼容协议


def test_normalize_protocol_rejects_unknown():
    with pytest.raises(ValueError):
        llm_client.normalize_protocol("gemini")


# ———————————————————— build_request：请求形态 ————————————————————

def test_build_request_openai_shape():
    url, headers, body = llm_client.build_request(
        "https://api.example.test/v1/", "sk-k", "m1", "openai", "hi",
        temperature=0.2, json_mode=True,
    )
    assert url == "https://api.example.test/v1/chat/completions"  # 尾斜杠归一
    assert headers == {"Authorization": "Bearer sk-k"}
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.2
    assert "max_tokens" not in body  # 业务调用不传则不带（与既有行为一致）


def test_build_request_anthropic_shape():
    url, headers, body = llm_client.build_request(
        "https://api.deepseek.com/anthropic", "sk-k", "m1", "anthropic", "hi",
        max_tokens=8,
    )
    # 与官方 SDK base_url 拼接口径一致：网关前缀 + /v1/messages
    assert url == "https://api.deepseek.com/anthropic/v1/messages"
    assert headers["x-api-key"] == "sk-k"
    assert headers["anthropic-version"] == llm_client.ANTHROPIC_VERSION
    assert "Authorization" not in headers  # anthropic 不认 Bearer
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 8  # 必填，未传时由适配层补默认值


def test_build_request_anthropic_fills_default_max_tokens():
    _, _, body = llm_client.build_request("https://x.test", "k", "m", "anthropic", "hi")
    assert body["max_tokens"] == llm_client.ANTHROPIC_DEFAULT_MAX_TOKENS > 0


# ———————————————————— parse_response：解析与 usage 归一化 ————————————————————

def _resp(payload, status_code=200):
    return httpx.Response(status_code, json=payload)


def test_parse_response_openai():
    text, usage = llm_client.parse_response("openai", _resp({
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11,
                  "prompt_tokens_details": {"cached_tokens": 4}},
    }))
    assert text == "OK"
    assert usage == {"prompt_tokens": 9, "completion_tokens": 2, "cached_tokens": 4, "total_tokens": 11}


def test_parse_response_openai_missing_choices_keeps_usage():
    """结构异常但 usage 可提取：LLMFormatError.usage 带回，parse_fail 不丢账。"""
    with pytest.raises(llm_client.LLMFormatError) as ei:
        llm_client.parse_response("openai", _resp({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}))
    assert ei.value.usage["total_tokens"] == 10


def test_parse_response_anthropic_maps_usage():
    """input+cache_creation+cache_read = 账单口径 prompt；cache_read 记 cached。"""
    text, usage = llm_client.parse_response("anthropic", _resp({
        "content": [{"type": "text", "text": '{"a":1}'}],
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                  "cache_read_input_tokens": 20, "output_tokens": 7},
    }))
    assert text == '{"a":1}'
    assert usage == {"prompt_tokens": 35, "completion_tokens": 7, "cached_tokens": 20, "total_tokens": 42}


def test_parse_response_anthropic_thinking_then_text_returns_text():
    """思考型模型正常响应（thinking + text 两块）：跳过 thinking 取 text 正文。"""
    text, usage = llm_client.parse_response("anthropic", _resp({
        "content": [
            {"type": "thinking", "thinking": "The user wants exactly two letters: OK",
             "signature": "sig"},
            {"type": "text", "text": "OK"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 37, "output_tokens": 19},
    }))
    assert text == "OK"
    assert usage["prompt_tokens"] == 37 and usage["completion_tokens"] == 19


def test_parse_response_anthropic_thinking_only_truncated_actionable_message():
    """max_tokens 过小被思考耗尽（DeepSeek deepseek-flash 实测形态）：报错必须
    指向「增大 max_tokens/换非思考模型」而非泛化的无 text 块，且 usage 不丢账。"""
    with pytest.raises(llm_client.LLMFormatError) as ei:
        llm_client.parse_response("anthropic", _resp({
            "content": [{"type": "thinking", "thinking": "We need answer only two letters OK",
                         "signature": "sig"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 37, "output_tokens": 8},
        }))
    assert "max_tokens" in str(ei.value) and "thinking" in str(ei.value)
    assert ei.value.usage["completion_tokens"] == 8


def test_parse_response_anthropic_no_text_block_keeps_usage():
    with pytest.raises(llm_client.LLMFormatError) as ei:
        llm_client.parse_response("anthropic", _resp({
            "content": [{"type": "tool_use"}],
            "usage": {"input_tokens": 12, "output_tokens": 1},
        }))
    assert ei.value.usage["prompt_tokens"] == 12


def test_parse_response_non_json():
    resp = httpx.Response(200, text="<html>boom</html>")
    with pytest.raises(llm_client.LLMFormatError) as ei:
        llm_client.parse_response("openai", resp)
    assert ei.value.usage == {}  # 整体非 JSON → 记 0（无从得知消耗）


# ———————— 网关变形响应与脏值（2026-09-12 审计修复回归） ————————

def test_parse_response_anthropic_content_plain_string():
    """简陋网关把 content 回成纯字符串：应成功解析而非误判 parse_fail（token 已花）。"""
    text, usage = llm_client.parse_response("anthropic", _resp({
        "content": "hello world",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }))
    assert text == "hello world"
    assert usage["prompt_tokens"] == 5


def test_parse_response_anthropic_content_mixed_blocks_order():
    """裸字符串项与规范 text block 混合：按原顺序取首个。"""
    text, _ = llm_client.parse_response("anthropic", _resp({
        "content": ["raw text", {"type": "text", "text": "block text"}],
    }))
    assert text == "raw text"


def test_parse_response_garbage_usage_raises_format_error_not_bare():
    """usage 脏值（字符串值/列表）必须收敛为 LLMFormatError——管理端只接该类型，
    裸 ValueError/AttributeError 逃逸会变 500。"""
    dirty = [
        ("anthropic", {"content": [{"type": "text", "text": "x"}], "usage": {"input_tokens": "many"}}),
        ("anthropic", {"content": [{"type": "text", "text": "x"}], "usage": "oops"}),
        ("openai", {"choices": [{"message": {"content": "x"}}], "usage": [1, 2]}),
        ("openai", {"choices": [{"message": {"content": "x"}}], "usage": "oops"}),
    ]
    for proto, body in dirty:
        with pytest.raises(llm_client.LLMFormatError) as ei:
            llm_client.parse_response(proto, _resp(body))
        assert ei.value.usage == {}


def test_chat_invalid_url_raises_api_error():
    """非法端口/URL 抛 InvalidURL（非 HTTPError 子类），chat 必须归类为 LLMAPIError。"""
    with pytest.raises(llm_client.LLMAPIError):
        llm_client.chat("https://host:notaport/", "k", "m", "openai", "hi", timeout=3)


# ———————————————————— chat：异常分型 ————————————————————

def test_chat_non_200_raises_api_error(monkeypatch):
    monkeypatch.setattr(llm_client.httpx, "post", lambda *a, **k: httpx.Response(500, text="boom"))
    with pytest.raises(llm_client.LLMAPIError) as ei:
        llm_client.chat("https://x.test", "k", "m", "anthropic", "hi", max_tokens=8)
    assert "500" in str(ei.value)


def test_chat_connect_error_raises_api_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(llm_client.httpx, "post", boom)
    with pytest.raises(llm_client.LLMAPIError):
        llm_client.chat("https://x.test", "k", "m", "openai", "hi")


def test_chat_anthropic_end_to_end(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json)
        return _resp({"content": [{"type": "text", "text": "你好"}],
                      "usage": {"input_tokens": 5, "output_tokens": 2}})

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    text, usage = llm_client.chat("https://x.test/anthropic", "k", "m", "anthropic", "hi")
    assert text == "你好"
    assert captured["url"].endswith("/v1/messages")
    assert usage["prompt_tokens"] == 5 and usage["completion_tokens"] == 2


# ———————— 事件 LLM 总结接线（2026-09-12） ————————

def _member(title: str, source: str = "IT之家"):
    """瞬态 Article：_rebuild_event_content 只读 title/source/publish_time/summary。"""
    from datetime import datetime, timezone

    from app.models.article import Article

    return Article(title=title, source=source, publish_time=datetime(2026, 9, 12, tzinfo=timezone.utc))


def test_rebuild_event_content_passes_effective_llm(db, monkeypatch):
    """接线回归：重算必须把生效 LLM 配置（含协议）传给 _llm_event_summary——
    此前漏传该参数，事件 LLM 总结自创建起从未生效（死代码）。"""
    from app.models.event import HotEvent
    from app.services import event_service

    s = db.factory()
    for k, v in (("llm_base_url", "https://x.test/anthropic"), ("llm_api_key", "k"),
                 ("llm_model", "m"), ("llm_protocol", "anthropic")):
        s.add(config_service.SystemSetting(key=k, value=v))
    s.commit()

    captured = {}

    def fake_summary(title, members, llm=None, event_id=None):
        captured["llm"] = llm
        return ("LLM 事件总结。", [])  # impacts 缺失 → 应回落事实拼装

    monkeypatch.setattr(event_service, "_llm_event_summary", fake_summary)
    ev = HotEvent(title="旧标题")
    members = [_member("报道A"), _member("报道B", "快科技")]
    event_service._rebuild_event_content(s, ev, members)

    assert captured["llm"] == ("https://x.test/anthropic", "k", "m", "anthropic")
    assert ev.summary == "LLM 事件总结。"
    assert ev.impacts and ev.impacts == event_service._compose_impacts(members)
    assert ev.timeline


def test_rebuild_event_content_falls_back_when_unconfigured(db, monkeypatch):
    from app.core.config import get_settings
    from app.models.event import HotEvent
    from app.services import event_service

    # 隔离 .env：未配置 = 空四元组（防宿主机 .env 漂移导致断言不稳）
    for k in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(get_settings(), k, "")
    monkeypatch.setattr(get_settings(), "llm_protocol", "openai")

    captured = {}

    def fake_summary(title, members, llm=None, event_id=None):
        captured["llm"] = llm
        return None  # 未配置/失败 → 降级事实拼装

    monkeypatch.setattr(event_service, "_llm_event_summary", fake_summary)
    ev = HotEvent(title="t")
    members = [_member("报道A"), _member("报道B", "快科技")]
    event_service._rebuild_event_content(db.factory(), ev, members)

    assert captured["llm"] == ("", "", "", "openai")
    assert ev.summary == event_service._compose_summary(members, anchor_title=ev.title)


def test_llm_event_summary_empty_summary_recorded_parse_fail(monkeypatch):
    """LLM 返回空 summary：不落 success（token 已花按 parse_fail 入账），
    返回 None 让调用方降级事实拼装——防线在 success 落账之前。"""
    import types

    from app.services import event_service

    # URL 守卫私网豁免（同 conftest 的 open_url_guard 语义）：离线 DNS 不可控，
    # 豁免后守卫只做格式校验，端点确定走到 httpx 桩
    monkeypatch.setattr(
        "app.utils.url_guard.get_settings",
        lambda: types.SimpleNamespace(allow_private_ai_urls=True),
    )

    class _R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"summary":"   ","impacts":["x"]}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}}

    monkeypatch.setattr(event_service.llm_client.httpx, "post", lambda *a, **k: _R())
    captured = {}
    monkeypatch.setattr(event_service, "record_usage", lambda **kw: captured.setdefault("s", kw))
    out = event_service._llm_event_summary(
        "标题", [_member("A")], llm=("https://llm.example.test", "k", "m", "openai"), event_id=7,
    )
    assert out is None
    assert captured["s"]["status"] == "parse_fail"
    assert "empty summary" in (captured["s"]["error"] or "")
    assert captured["s"]["total_tokens"] == 13  # 花掉的 token 不丢账


# ———————————————————— 配置层：llm_protocol ————————————————————

def test_effective_llm_returns_protocol_and_sanitizes_dirty(db, monkeypatch):
    """生效四元组带 protocol；DB 脏值（手改）/ .env 空值回落 openai 不抛错。"""
    s = db.factory()
    try:
        s.add(config_service.SystemSetting(key="llm_base_url", value="https://x.test"))
        s.add(config_service.SystemSetting(key="llm_api_key", value="k"))
        s.add(config_service.SystemSetting(key="llm_model", value="m"))
        s.add(config_service.SystemSetting(key="llm_protocol", value="Anthropic"))  # 手滑大小写
        s.commit()
        base, key, model, protocol = config_service.effective_llm(s)
    finally:
        s.close()
    assert (base, key, model, protocol) == ("https://x.test", "k", "m", "anthropic")


def test_set_overrides_rejects_bad_protocol(db):
    s = db.factory()
    try:
        with pytest.raises(ValueError):
            config_service.set_overrides(s, {"llm_protocol": "gemini"})
        config_service.set_overrides(s, {"llm_protocol": "anthropic"})
        assert config_service.get_effective(s, "llm_protocol") == "anthropic"
    finally:
        s.close()
