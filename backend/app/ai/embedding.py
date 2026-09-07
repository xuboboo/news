"""Embedding 服务（PRD 第 8 节）

两级实现：
1. API 模式：配置 embedding_base_url 等后走 OpenAI 兼容 /embeddings 接口。
2. 本地模式：字符级 n-gram 哈希向量化（256 维，L2 归一化）——确定性、可离线，
   对同事件报道的标题/正文具有稳定词面相似度，支撑 pgvector 近邻检索。

安全/一致性：API 模式默认严格（EMBEDDING_STRICT=true）——调用失败或返回维度
与 EMBED_DIM 不一致时记录 api_fail 并抛 EmbeddingError（由任务层重试），
绝不静默降采样或混入本地哈希向量污染聚类；仅"未配置 API"走本地哈希。
"""
import hashlib
import math
import re
import time
from typing import List, Optional

import httpx

from app.core.config import get_settings
from app.services.usage_service import extract_openai_usage, record_usage
from app.utils.url_guard import assert_public_http_url

_TOKEN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


class EmbeddingError(RuntimeError):
    """Embedding API 调用失败（含维度不符）；strict 模式下抛出，由任务层重试。"""


def _ngrams(text: str, n_range=(1, 2, 3)) -> List[str]:
    grams: List[str] = []
    for token in _TOKEN.findall((text or "").lower()):
        for n in n_range:
            if len(token) >= n:
                grams.extend(token[i: i + n] for i in range(len(token) - n + 1))
    return grams


def _hash_vec(text: str, dim: int) -> List[float]:
    """n-gram 哈希向量化：md5 散列落桶 + 频次累加 + L2 归一化。"""
    vec = [0.0] * dim
    for g in _ngrams(text):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _api_available() -> bool:
    s = get_settings()
    return bool(s.embedding_base_url and s.embedding_api_key and s.embedding_model)


def embed(
    text: str,
    cfg: tuple[str, str, str] | None = None,
    article_id: int | None = None,
    trigger: str = "auto",
) -> List[float]:
    """生成文本向量（标题 + 正文前段联合编码），并负责用量记账（每次调用恰好落一条）：

    - API 配置启用且成功 → status=success（仅输入 token）；
    - API 配置启用但失败（连接/非 200/维度不符）→ status=api_fail；
      strict 模式（默认）抛 EmbeddingError 交任务层重试；非 strict 静默回退本地哈希；
    - API 未配置 → 记 model=local-hash-256、cost=0（与抽取式摘要同理，非 api_fail）。

    cfg: 运行时生效配置 (base_url, api_key, model)，由调用方从配置服务读取；
    未传时回退 .env 静态配置。
    """
    s = get_settings()
    base_url, api_key, model = cfg if cfg else (
        s.embedding_base_url, s.embedding_api_key, s.embedding_model
    )
    if base_url and api_key and model:
        start = time.monotonic()
        usage: dict = {}  # 失败早于 usage 提取时保持空账（token 未消耗）
        try:
            base_url = assert_public_http_url(base_url)
            resp = httpx.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": text[:2000]},
                timeout=60,
            )
            resp.raise_for_status()
            body = resp.json()
            usage = extract_openai_usage(body)  # 先取 usage 再做后续校验
            vec = body["data"][0]["embedding"]
        except Exception as exc:
            record_usage(
                task_type="embedding",
                model=model,
                status="api_fail",
                error=str(exc),
                latency_ms=int((time.monotonic() - start) * 1000),
                article_id=article_id,
                trigger=trigger,
                **usage,
            )
            if s.embedding_strict:
                raise EmbeddingError(f"Embedding API 调用失败：{exc}") from exc
            return _hash_vec(text, s.embed_dim)
        if len(vec) != s.embed_dim:
            err = (
                f"Embedding 服务返回维度 {len(vec)} 与 EMBED_DIM={s.embed_dim} 不一致，"
                "拒绝写入（降采样/平铺会破坏向量语义、污染事件聚类）"
            )
            record_usage(
                task_type="embedding",
                model=model,
                status="api_fail",
                error=err,
                latency_ms=int((time.monotonic() - start) * 1000),
                article_id=article_id,
                trigger=trigger,
                **usage,
            )
            if s.embedding_strict:
                raise EmbeddingError(err)
            return _hash_vec(text, s.embed_dim)
        record_usage(
            task_type="embedding",
            model=model,
            status="success",
            latency_ms=int((time.monotonic() - start) * 1000),
            article_id=article_id,
            trigger=trigger,
            **usage,
        )
        return [float(v) for v in vec]
    # API 未配置：本地哈希回退落一条 cost=0 的账（status=success，非 api_fail）
    record_usage(
        task_type="embedding",
        model="local-hash-256",
        status="success",
        article_id=article_id,
        trigger=trigger,
    )
    return _hash_vec(text, s.embed_dim)


def cosine(a: Optional[List[float]], b: Optional[List[float]]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def centroid(vectors: List[List[float]]) -> List[float]:
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    norm = math.sqrt(sum(x * x for x in acc)) or 1.0
    return [x / norm for x in acc]
