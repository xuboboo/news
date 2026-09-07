"""AI 摘要服务（PRD 第 5/7 节）

两级实现：
1. LLM 模式：配置 llm_base_url / llm_api_key / llm_model 后走 OpenAI 兼容接口，
   Prompt 按 PRD 要求输出 {"summary","points","tags"}，客观、不增加观点、保留事实。
2. 抽取式兜底：未配置 LLM 时使用 TextRank 式抽取算法（词频+位置+标题相关性打分），
   同样产出三要素，保证流水线闭环可运行。
"""
import json
import re
import time
from dataclasses import dataclass, field
from typing import List

import httpx

from app.core.config import get_settings
from app.services.usage_service import extract_openai_usage, record_usage
from app.utils.text import extract_keywords, is_boilerplate, split_sentences, truncate
from app.utils.url_guard import assert_public_http_url

PROMPT_TEMPLATE = """你是科技资讯摘要助手。请阅读以下文章，输出严格的 JSON（不要输出任何其他文字）：
{{"summary":"一句话摘要(50字内)","points":["核心信息",...],"tags":["标签",...]}}

要求：
- 客观、不增加观点、只保留事实
- points 输出 3-5 条
- tags 输出 2-5 个

标题：{title}
正文：
{content}"""


@dataclass
class SummaryResult:
    summary_short: str = ""
    summary_long: str = ""
    key_points: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    model: str = ""


# 无效文本（致谢行 / 图片元信息）——摘要与要点统一过滤
_DIRTY_SUMMARY = re.compile(r"线索投递|^(感谢|题图|配图|图片来源)")


def _llm_available(llm: tuple[str, str, str] | None = None) -> bool:
    if llm is None:
        s = get_settings()
        llm = (s.llm_base_url, s.llm_api_key, s.llm_model)
    return bool(llm[0] and llm[1] and llm[2])


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def summarize(
    title: str,
    content: str,
    llm: tuple[str, str, str] | None = None,
    article_id: int | None = None,
    trigger: str = "auto",
) -> SummaryResult:
    """生成摘要：优先 LLM，失败或未配置时走抽取式算法；统一做兜底校验。

    llm: 运行时生效配置 (base_url, api_key, model)，由调用方从配置服务读取，
    未传时回退 .env 静态配置。
    article_id / trigger: 透传给用量记账（ai_usage_log），便于按文章/来源追溯。
    """
    if _llm_available(llm):
        result = _summarize_by_llm(title, content, llm, article_id=article_id, trigger=trigger)
        if result is None:
            result = _summarize_extractive(title, content)
    else:
        # 未配置 LLM：抽取式兜底也落一条账（token/cost=0），供 LLM 覆盖率统计
        result = _summarize_extractive(title, content)
        record_usage(
            task_type="summary",
            model=result.model,
            status="success",
            article_id=article_id,
            trigger=trigger,
        )

    # 兜底：摘要为空或为致谢行等无效文本时，退回标题；要点中剔除同类文本
    if not result.summary_short.strip() or _DIRTY_SUMMARY.search(result.summary_short.strip()):
        result.summary_short = truncate(title, 60)
    result.key_points = [p for p in result.key_points if not _DIRTY_SUMMARY.search(p.strip())]
    if not result.key_points:
        result.key_points = [truncate(title, 70)]
    return result


def _summarize_by_llm(
    title: str,
    content: str,
    llm: tuple[str, str, str],
    article_id: int | None = None,
    trigger: str = "auto",
) -> SummaryResult | None:
    """调用 LLM 生成摘要。内部负责用量记账（每次调用恰好落一条 ai_usage_log）：

    - HTTP 200 且解析成功 → status=success（token 照记）；
    - HTTP 200 但响应解析 / JSON 提取失败 → status=parse_fail（token 已花，必须入账）；
    - 连接异常 / 非 200 → status=api_fail。
    返回 None 表示调用失败，由调用方降级抽取式（业务零感知）。
    """
    base_url, api_key, model = llm
    prompt = PROMPT_TEMPLATE.format(title=title, content=(content or title)[:4000])
    start = time.monotonic()
    http_ok = False  # 是否已收到 HTTP 200（区分 api_fail / parse_fail）
    usage = {}
    try:
        base_url = assert_public_http_url(base_url)  # 违规 → api_fail 落账后降级抽取式
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        http_ok = True
        body = resp.json()
        # 第一件事提取 usage：即使后续解析失败，token 也已消耗
        usage = extract_openai_usage(body)
        raw = body["choices"][0]["message"]["content"]
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        record_usage(
            task_type="summary",
            model=model,
            status="success",
            latency_ms=_elapsed_ms(start),
            article_id=article_id,
            trigger=trigger,
            **usage,
        )
        return SummaryResult(
            summary_short=truncate(str(data.get("summary", "")), 80),
            summary_long=truncate(str(data.get("summary", "")), 400),
            key_points=[str(p) for p in (data.get("points") or [])][:5],
            tags=[str(t) for t in (data.get("tags") or [])][:5],
            model=model,
        )
    except Exception as exc:
        record_usage(
            task_type="summary",
            model=model,
            status="parse_fail" if http_ok else "api_fail",
            error=str(exc),
            latency_ms=_elapsed_ms(start),
            article_id=article_id,
            trigger=trigger,
            **usage,
        )
        return None


def _summarize_extractive(title: str, content: str) -> SummaryResult:
    """抽取式摘要：句子打分 = 关键词覆盖 × 位置权重，取 TopN 作为核心信息。"""
    text = (content or "").strip() or title
    sents = [s for s in split_sentences(text) if not is_boilerplate(s)]
    kws = extract_keywords(f"{title}。{text}", 12)
    title_kws = set(extract_keywords(title, 6))

    scored: List[tuple] = []
    for i, sent in enumerate(sents[:40]):
        words = set(extract_keywords(sent, 20))
        overlap = len(words & (title_kws | set(kws))) / max(1, len(kws))
        pos_weight = 1.25 if i < 3 else 1.0  # 导语位置加权
        scored.append((overlap * pos_weight, i, sent))
    scored.sort(reverse=True)

    # 取打分最高的 5 句，按原文顺序输出为核心信息
    key_points = [truncate(sent, 70) for _, _, sent in sorted(scored[:5], key=lambda t: t[1])]
    if not key_points:
        key_points = [truncate(title, 70)]

    lead = sents[0] if sents else title
    summary_short = truncate(lead, 60)

    tags = kws[:4] if kws else []
    return SummaryResult(
        summary_short=summary_short,
        summary_long=truncate(text, 400),
        key_points=key_points[:5],
        tags=tags,
        model="extractive-textrank-v1",
    )
