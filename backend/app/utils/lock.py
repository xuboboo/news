"""基于 Redis 的粗粒度互斥锁（SET NX PX）。

用途：crawler 有两个触发入口——常驻 loop 与管理面板手动 crawler_task，
无互斥时会并发抓取同一批 URL，靠唯一约束兜底会浪费且制造无谓冲突。
锁 TTL 仅用于持有者进程崩溃后的自动释放；正常路径用完即删。
释放走 compare-and-delete（L-1，2026-09-18 审计整改）：value 存随机持有者
token，只删自己持有的锁——否则 A 超 TTL 后被 B 接管、A 收尾时误删 B 的锁，
互斥保证在「慢任务 + TTL 边界」下不成立。
"""
import logging
import secrets

import redis as redis_lib

from app.core.config import get_settings

logger = logging.getLogger(__name__)

LOCK_CRAWLER = "crawler"
# 双来源（IT之家 + 快科技，crawler/multi.py）单轮最坏耗时：入口上限合计约 900 次文章页
# 请求（ithome ≤240 + 快科技三入口 ≤600 + feed）× 15s 按阶段超时，站点整体变慢时可超 1h；
# TTL 仅兜底进程崩溃（崩溃后最长这么久才自动放行），数据正确性由 URL 唯一约束 + SAVEPOINT 兜底
LOCK_CRAWLER_TTL_MS = 60 * 60 * 1000

LOCK_STATS = "stats"
# 最坏轮次 = 批次 50 × 2 请求 × 16s（8s 按阶段超时的最坏展开）+ 间隔 ≈ 28min ≈ 30min TTL；
# TTL 仅兜底进程崩溃
LOCK_STATS_TTL_MS = 30 * 60 * 1000

LOCK_EVENT = "event"
# 聚类 + 事件间二阶段合并非原子读改写，beat(每10min) 与手动触发并发会产生重复
# 事件；单 worker 串行下是双保险，扩多 worker 后是正确性前提。
# 最坏轮次 = 全量 LLM 重算（60s/次 × 新增成员事件数），TTL 与 visibility_timeout
# 同口径放宽到 4h，仅兜底进程崩溃
LOCK_EVENT_TTL_MS = 4 * 60 * 60 * 1000


def _client() -> "redis_lib.Redis":
    return redis_lib.Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2
    )


# Redis 不可用时的哨兵 token：truthy 不阻塞主链路，release 时跳过删除
_REDIS_BYPASS = "redis-bypass"

# 仅当持有者 token 匹配才删除（原子比较+删）
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def acquire_lock(name: str, ttl_ms: int) -> str | None:
    """尝试抢锁：成功返回持有者 token（调用方须传给 release_lock），
    已被其他进程持有返回 None。truthy/falsy 兼容既有 `if not acquire_lock(...)`
    布尔用法。"""
    token = secrets.token_hex(16)
    try:
        if _client().set(f"lock:{name}", token, nx=True, px=ttl_ms):
            return token
        return None
    except Exception:
        # Redis 不可用时不阻塞采集主链路：放行并依赖 URL 唯一约束兜底
        logger.warning("acquire lock %s failed (redis unavailable), proceeding", name, exc_info=True)
        return _REDIS_BYPASS


def release_lock(name: str, token: str | None) -> None:
    """释放自己持有的锁；token 不匹配（锁已超时被他人接管）时不动别人的锁。"""
    if not token or token == _REDIS_BYPASS:
        return
    try:
        deleted = _client().eval(_RELEASE_LUA, 1, f"lock:{name}", token)
        if not deleted:
            logger.warning(
                "release lock %s: token mismatch (锁已超时被接管), 跳过删除", name
            )
    except Exception:
        logger.warning("release lock %s failed", name, exc_info=True)
