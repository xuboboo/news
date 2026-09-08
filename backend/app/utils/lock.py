"""基于 Redis 的粗粒度互斥锁（SET NX PX）。

用途：crawler 有两个触发入口——常驻 loop 与管理面板手动 crawler_task，
无互斥时会并发抓取同一批 URL，靠唯一约束兜底会浪费且制造无谓冲突。
锁 TTL 仅用于持有者进程崩溃后的自动释放；正常路径用完即删。
"""
import logging

import redis as redis_lib

from app.core.config import get_settings

logger = logging.getLogger(__name__)

LOCK_CRAWLER = "crawler"
# 采集单轮理论耗时 ≪ 30min（每源限流 + 每篇 15s 超时）；TTL 仅兜底进程崩溃
LOCK_CRAWLER_TTL_MS = 30 * 60 * 1000

LOCK_STATS = "stats"
# 最坏轮次 = 批次 50 × 2 请求 × 8s 超时 + 间隔 ≈ 14min ≪ 30min；TTL 仅兜底进程崩溃
LOCK_STATS_TTL_MS = 30 * 60 * 1000


def _client() -> "redis_lib.Redis":
    return redis_lib.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2
    )


def acquire_lock(name: str, ttl_ms: int) -> bool:
    """尝试抢锁：成功返回 True，已被其他进程持有返回 False。"""
    try:
        return bool(_client().set(f"lock:{name}", "1", nx=True, px=ttl_ms))
    except Exception:
        # Redis 不可用时不阻塞采集主链路：放行并依赖 URL 唯一约束兜底
        logger.warning("acquire lock %s failed (redis unavailable), proceeding", name, exc_info=True)
        return True


def release_lock(name: str) -> None:
    try:
        _client().delete(f"lock:{name}")
    except Exception:
        logger.warning("release lock %s failed", name, exc_info=True)
