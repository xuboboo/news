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
