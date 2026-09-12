"""AI 摘要服务（PRD 第 5/7 节）

两级实现：
1. LLM 模式：配置 llm_base_url / llm_api_key / llm_model / llm_protocol 后经
   llm_client 调用（OpenAI 兼容 / Anthropic 原生双协议），
   Prompt 按 PRD 要求输出 {"summary","points","tags"}，客观、不增加观点、保留事实。
2. 抽取式兜底（extractive-textrank-v2）：真 TextRank——句子相似图（Mihalcea-Tarau
   重叠度）上跑 PageRank，混合标题相关覆盖率与导语位置权重，MMR 去冗余选 5 句，
   同样产出三要素，保证流水线闭环可运行。
"""
import math
import re
import time
from dataclasses import dataclass, field
from typing import List

from app.ai import llm_client
from app.core.config import get_settings
from app.services.usage_service import record_usage
from app.utils.text import extract_keywords, is_boilerplate, split_sentences, truncate
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

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


def _llm_available(llm: tuple[str, str, str, str] | None = None) -> bool:
    if llm is None:
        s = get_settings()
        # .env 协议脏值回落 openai（与 effective_llm 同口径），不让配置手滑降级摘要链路
        protocol = (s.llm_protocol or "").strip().lower()
        if protocol not in llm_client.PROTOCOLS:
            protocol = "openai"
        llm = (s.llm_base_url, s.llm_api_key, s.llm_model, protocol)
    return bool(llm[0] and llm[1] and llm[2])


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def summarize(
    title: str,
    content: str,
    llm: tuple[str, str, str, str] | None = None,
    article_id: int | None = None,
    trigger: str = "auto",
) -> SummaryResult:
    """生成摘要：优先 LLM，失败或未配置时走抽取式算法；统一做兜底校验。

    llm: 运行时生效配置 (base_url, api_key, model, protocol)，由调用方从配置
    服务读取，未传时回退 .env 静态配置。
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
    llm: tuple[str, str, str, str],
    article_id: int | None = None,
    trigger: str = "auto",
) -> SummaryResult | None:
    """调用 LLM 生成摘要。内部负责用量记账（每次尝试各落一条 ai_usage_log）：

    - HTTP 200 且解析成功 → status=success（token 照记）；
    - HTTP 200 但 JSON 提取失败 → status=parse_fail（token 已花，必须入账），
      并带纠错反馈自动重试一次——思考型模型偶发残缺 JSON（2026-09-12 文章266
      实测：第 346 字符处语法错误，整篇永久降级抽取式），重试可挽回；
    - 连接异常 / 非 200 / URL 守卫拦截 → status=api_fail（连接层失败重试
      意义有限，不重试）。
    返回 None 表示调用失败，由调用方降级抽取式（业务零感知）。
    """
    base_url, api_key, model, protocol = llm
    prompt = PROMPT_TEMPLATE.format(title=title, content=(content or title)[:4000])
    start = time.monotonic()

    def _record(status: str, error: str | None, **tokens) -> None:
        record_usage(
            task_type="summary",
            model=model,
            status=status,
            error=error,
            latency_ms=_elapsed_ms(start),
            article_id=article_id,
            trigger=trigger,
            **tokens,
        )

    try:
        base_url = assert_public_http_url(base_url)  # 违规 → api_fail 落账后降级抽取式
    except UrlNotAllowed as exc:
        _record("api_fail", str(exc))
        return None

    data = None
    usage: dict = {}
    feedback = ""
    for attempt in range(2):
        try:
            raw, usage = llm_client.chat(
                base_url, api_key, model, protocol, prompt + feedback,
                temperature=0.2, json_mode=True,
            )
            data = llm_client.extract_json_object(raw)
            break
        except llm_client.LLMAPIError as exc:
            _record("api_fail", str(exc), **usage)
            return None
        except (ValueError, llm_client.LLMFormatError) as exc:
            # JSON 提取失败 / 响应结构异常：本次 token 如实落账（LLMFormatError
            # 的 usage 由异常带回，chat 未返回），末次失败后由调用方降级
            usage = getattr(exc, "usage", None) or usage
            _record("parse_fail", str(exc), **usage)
            feedback = llm_client.JSON_RETRY_HINT.format(error=exc)
    if data is None:
        return None
    _record("success", None, **usage)
    return SummaryResult(
        summary_short=truncate(str(data.get("summary", "")), 80),
        summary_long=truncate(str(data.get("summary", "")), 400),
        # 容器类型守卫：模型把 points/tags 返回成字符串时逐字符拆分会产出
        # 单字要点污染摘要，dict 会产出键名——非 list 一律按缺失处理
        key_points=[str(p) for p in data.get("points")][:5] if isinstance(data.get("points"), list) else [],
        tags=[str(t) for t in data.get("tags")][:5] if isinstance(data.get("tags"), list) else [],
        model=model,
    )


def _pagerank(sim: List[List[float]], d: float = 0.85, iters: int = 30) -> List[float]:
    """加权 PageRank（TextRank 句子重要度）；悬挂节点均匀分摊。n≤40，微秒级。"""
    n = len(sim)
    if n == 0:
        return []
    out = [sum(row) for row in sim]
    rank = [1.0 / n] * n
    for _ in range(iters):
        nxt = [(1.0 - d) / n] * n
        dangling = d * sum(r for r, o in zip(rank, out) if o <= 0) / n
        for i in range(n):
            if out[i] <= 0:
                continue
            w = d * rank[i] / out[i]
            row = sim[i]
            for j in range(n):
                if row[j] > 0:
                    nxt[j] += w * row[j]
        for j in range(n):
            nxt[j] += dangling
        rank = nxt
    return rank


def _mmr_select(scores: List[float], word_sets: List[set], k: int = 5, lam: float = 0.7) -> List[int]:
    """MMR 选句：λ·相关性 − (1−λ)·与已选句的最大词集 Jaccard，抑制近重复句挤占要点。"""

    def _jac(a: set, b: set) -> float:
        return len(a & b) / len(a | b) if a | b else 0.0

    cand = set(range(len(scores)))
    picked: List[int] = []
    while cand and len(picked) < k:
        best_i, best_v = None, -1.0
        for i in cand:
            red = max((_jac(word_sets[i], word_sets[p]) for p in picked), default=0.0)
            v = lam * scores[i] - (1.0 - lam) * red
            if v > best_v:
                best_i, best_v = i, v
        picked.append(best_i)
        cand.remove(best_i)
    return picked


def _summarize_extractive(title: str, content: str) -> SummaryResult:
    """抽取式摘要 v2：TextRank 句子重要度 × 标题相关覆盖率 × 导语位置权重，
    MMR 去冗余后取 Top5 按原文顺序输出。"""
    text = (content or "").strip() or title
    sents = [s for s in split_sentences(text) if not is_boilerplate(s)][:40]
    kws = extract_keywords(f"{title}。{text}", 12)
    title_kws = set(extract_keywords(title, 6))
    ref = title_kws | set(kws)

    word_sets = [set(extract_keywords(s, 20)) for s in sents]
    n = len(sents)

    # 覆盖率用 precision 语义（分母 = 句子自身关键词数）：长句不再天然占优
    cov = [len(w & ref) / len(w) if w else 0.0 for w in word_sets]

    # TextRank：句子相似图（重叠度按词数对数归一，Mihalcea-Tarau 2004）上跑 PageRank
    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            ov = len(word_sets[i] & word_sets[j])
            if ov and word_sets[i] and word_sets[j]:
                denom = math.log(len(word_sets[i])) + math.log(len(word_sets[j]))
                if denom > 0:
                    sim[i][j] = sim[j][i] = ov / denom
    rank = _pagerank(sim)
    max_rank = max(rank, default=0.0)
    rank = [r / max_rank if max_rank > 0 else 0.0 for r in rank]

    pos_w = [1.25 if i < 3 else 1.0 for i in range(n)]  # 导语位置加权
    scores = [(0.7 * rank[i] + 0.3 * cov[i]) * pos_w[i] for i in range(n)]

    picked = _mmr_select(scores, word_sets, k=5)
    key_points = [truncate(sents[i], 70) for i in sorted(picked)]
    if not key_points:
        key_points = [truncate(title, 70)]

    lead = sents[0] if sents else title
    summary_short = truncate(lead, 60)
    summary_long = (
        truncate("。".join(sents[i] for i in sorted(picked)), 400)
        if picked
        else truncate(text, 400)
    )

    tags = kws[:4] if kws else []
    return SummaryResult(
        summary_short=summary_short,
        summary_long=summary_long,
        key_points=key_points,
        tags=tags,
        model="extractive-textrank-v2",
    )
