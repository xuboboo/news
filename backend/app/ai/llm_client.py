"""LLM 客户端适配层：OpenAI 兼容 / Anthropic 原生双协议。

业务侧只关心「发一段 prompt 拿回文本 + token 用量」，协议差异收敛在这里：

- openai:    POST {base}/chat/completions，Bearer 认证；json_mode 走
             response_format；解析 choices[0].message.content；
- anthropic: POST {base}/v1/messages（与官方 SDK 的 base_url 拼接口径一致，
             适配 DeepSeek /anthropic、智谱 /api/anthropic 及各类中转网关），
             x-api-key + anthropic-version 认证，max_tokens 必填（默认补齐），
             解析 content 中首个 text 块；usage 映射为 OpenAI 四项口径
             （cache_creation 并入 prompt、cache_read 记 cached，与账单一致）。

异常分型供调用方按既有口径落账：
- LLMAPIError    连接失败 / HTTP 非 200 → api_fail
- LLMFormatError HTTP 200 但结构不符   → parse_fail

admin 的「测试连接」因需自定义 502 文案与审计明细，自行发请求但复用
build_request / parse_response，协议逻辑不重复实现。
"""
from typing import Tuple

import httpx

from app.services.usage_service import extract_openai_usage

ANTHROPIC_VERSION = "2023-06-01"
PROTOCOLS = ("openai", "anthropic")
# Anthropic 协议 max_tokens 必填；摘要/综述类 JSON 输出 1024 足够
ANTHROPIC_DEFAULT_MAX_TOKENS = 1024


class LLMAPIError(Exception):
    """连接失败或 HTTP 非 200（调用方落账 api_fail）。"""


class LLMFormatError(Exception):
    """HTTP 200 但响应结构不符（调用方落账 parse_fail，token 已花）。

    usage: 已提取到的归一化用量随异常带回（content 解析失败但 usage 可用时
    不丢账）；响应整体非 JSON 时为 None（记 0）。
    """

    def __init__(self, msg: str, usage: dict | None = None):
        super().__init__(msg)
        self.usage = usage or {}


def normalize_protocol(protocol: str) -> str:
    p = (protocol or "").strip().lower()
    if not p:
        return "openai"  # 空值 = 默认兼容协议（与 effective_llm 的容错同口径）
    if p in PROTOCOLS:
        return p
    raise ValueError(f"llm_protocol 仅支持 {'/'.join(PROTOCOLS)}（收到 {p[:32]!r}）")


def build_request(
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    prompt: str,
    *,
    system: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> Tuple[str, dict, dict]:
    """按协议构造 (url, headers, body)。不发送请求。"""
    if normalize_protocol(protocol) == "anthropic":
        body: dict = {
            "model": model,
            "max_tokens": int(max_tokens or ANTHROPIC_DEFAULT_MAX_TOKENS),
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if system:
            body["system"] = system
        return (
            f"{base_url.rstrip('/')}/v1/messages",
            {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
            body,
        )
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    body = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return (
        f"{base_url.rstrip('/')}/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        body,
    )


def extract_anthropic_usage(body: dict) -> dict:
    """Anthropic usage → OpenAI 四项口径（同一套落账与成本看板）。

    input_tokens 不含缓存段；账单口径输入总量 = input + cache_creation + cache_read，
    其中 cache_read 按缓存价计费记 cached，cache_write 全价并入 prompt。
    """
    u = (body or {}).get("usage") or {}
    prompt = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(
        u.get("cache_read_input_tokens") or 0
    )
    prompt = max(0, prompt)
    completion = int(u.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": max(0, completion),
        "cached_tokens": max(0, min(int(u.get("cache_read_input_tokens") or 0), prompt)),
        "total_tokens": prompt + max(0, completion),
    }


def parse_response(protocol: str, resp) -> Tuple[str, dict]:
    """从 HTTP 200 响应提取 (文本, 归一化 usage)；结构不符抛 LLMFormatError（带已提取 usage）。

    防线：第三方网关的变形响应（usage 脏值 / content 为纯字符串）一律收敛为
    LLMFormatError，不得抛 ValueError/AttributeError 之类裸异常——管理端测试
    连通只接 LLMFormatError，裸异常会变 500。
    """
    try:
        data = resp.json()
    except Exception as exc:
        raise LLMFormatError(f"响应非 JSON：{exc}") from exc
    if normalize_protocol(protocol) == "anthropic":
        try:
            usage = extract_anthropic_usage(data)
        except Exception as exc:
            raise LLMFormatError(f"usage 字段异常：{exc}") from exc
        blocks = data.get("content") if isinstance(data, dict) else None
        if isinstance(blocks, str):
            return blocks, usage  # 简陋网关直接回字符串形式
        # 规范 block 列表（type=text）与裸字符串项统一按序收集，取首个 text
        texts: list[str] = []
        for b in blocks or []:
            if isinstance(b, dict) and b.get("type") == "text":
                texts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                texts.append(b)
        if not texts:
            raise LLMFormatError("响应无 text 内容块", usage)
        return texts[0], usage
    try:
        usage = extract_openai_usage(data)
    except Exception as exc:
        raise LLMFormatError(f"usage 字段异常：{exc}") from exc
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMFormatError(f"响应缺少 choices/content：{exc}", usage) from exc
    return str(text or ""), usage


def chat(
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    prompt: str,
    *,
    system: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
    timeout: float = 60,
) -> Tuple[str, dict]:
    """单轮补全：返回 (文本, usage 四项)；连接/非 200 抛 LLMAPIError，结构不符抛 LLMFormatError。"""
    url, headers, body = build_request(
        base_url, api_key, model, protocol, prompt,
        system=system, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
    )
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL 不是 HTTPError 子类（如端口写错 'notaport'），漏接会击穿
        # 异常分型：业务侧误记 parse_fail、管理端测试连通 500 而非 502
        raise LLMAPIError(str(exc)) from exc
    if resp.status_code != 200:
        raise LLMAPIError(f"HTTP {resp.status_code}：{resp.text[:200]}")
    return parse_response(protocol, resp)
