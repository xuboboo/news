"""读接口 Redis 响应缓存（热榜 / 最新资讯专用，小而窄，不做通用框架）。

背景：每个登录客户端每 60 秒轮询 /articles/latest?limit=400 与 /hot/board，
每次请求都在 DB 内做全窗口 exp() 聚合 + 排序 + count。缓存后 DB 压力从
「每客户端每分钟」降为「每缓存键每 TTL」。

设计约束：
- 复用 infra_status 的模块级共享连接池（与告警巡检同池，不再新建 client）；
- Redis 不可达时静默直连 DB（照 auth/throttle.py 的降级模式），缓存故障
  不影响可用性，只退化为原有负载；
- 值为响应 dict 的 JSON 序列化（字段均已 isoformat 字符串化，天然可序列化）；
- TTL 到期自然失效，不做主动失效：底层数据节奏为采集 30min / 互动轮询 15min，
  45s 的缓存延迟相对无感。
"""
import json
import logging

from app.core.config import get_settings
from app.services.infra_status import redis_client

logger = logging.getLogger(__name__)


def _ttl() -> int:
    return max(0, int(get_settings().api_cache_ttl_seconds))


def cache_get_json(key: str) -> dict | None:
    """命中返回反序列化 dict；未命中/禁用/Redis 故障返回 None（调用方直连 DB）。"""
    ttl = _ttl()
    if ttl <= 0:
        return None
    try:
        raw = redis_client().get(key)
    except Exception:
        logger.warning("api_cache get failed（降级直连 DB）: key=%s", key)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # 反序列化失败（如手工改动/版本混写）：当未命中处理，稍后会被覆写
        return None


def cache_set_json(key: str, value: dict) -> None:
    ttl = _ttl()
    if ttl <= 0:
        return
    try:
        redis_client().setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        logger.warning("api_cache set failed: key=%s", key)
