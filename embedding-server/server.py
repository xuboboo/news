"""轻量 OpenAI 兼容 /embeddings 服务：fastembed(ONNX) + BAAI/bge-small-zh-v1.5（384 维）。

用途：给后端事件聚合提供真语义向量（本地哈希向量无判别力，实测不相关文章
余弦可达 0.86）。免 API key、免 GPU；模型经 HF_ENDPOINT 镜像站下载后缓存在
挂载卷 /cache，重启不重下。

后端侧配置（backend/.env）：
    EMBEDDING_BASE_URL=http://embedding:80/v1
    EMBEDDING_API_KEY=local-embedding   # 占位即可，服务不鉴权
    EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
    ALLOW_PRIVATE_AI_URLS=true          # 内网服务名，需豁免 SSRF 校验
"""
import os
import time
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI
from pydantic import BaseModel

MODEL_ID = os.environ.get("EMBED_MODEL_ID", "BAAI/bge-small-zh-v1.5")
CACHE_DIR = os.environ.get("EMBED_CACHE_DIR", "/cache")

_model = None
_lock = Lock()


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model=MODEL_ID, cache_dir=CACHE_DIR)
    return _model


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动即预热：首启需从镜像站下载模型（可能数分钟），完成后再对外健康
    get_model()
    yield


app = FastAPI(lifespan=lifespan)


class EmbedRequest(BaseModel):
    """OpenAI 兼容请求体；后端只发字符串 input，列表为兼容预留。"""

    input: str | list[str]
    model: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID}


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest):
    texts = [req.input] if isinstance(req.input, str) else [t for t in req.input]
    start = time.monotonic()
    vecs = [v.tolist() for v in get_model().embed(texts)]
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vecs)
        ],
        "model": req.model or MODEL_ID,
        # fastembed 不返回 token 数，按字符数粗估（仅入账展示用）
        "usage": {
            "prompt_tokens": sum(len(t) for t in texts),
            "total_tokens": sum(len(t) for t in texts),
        },
        "latency_ms": int((time.monotonic() - start) * 1000),
    }
