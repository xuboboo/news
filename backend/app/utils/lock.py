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
from dataclasses import dataclass
from enum import Enum

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


# Redis 不可用时的兼容哨兵 token。仅 crawler 可选择继续；非幂等任务必须使用 acquire_lock_result。
_REDIS_BYPASS = "redis-bypass"


class LockState(str, Enum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LockResult:
    state: LockState
    token: str | None = None

    def __bool__(self) -> bool:
        return self.state == LockState.ACQUIRED


# 仅当持有者 token 匹配才删除（原子比较+删）
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def acquire_lock_result(name: str, ttl_ms: int) -> LockResult:
    token = secrets.token_hex(16)
    try:
        if _client().set(f"lock:{name}", token, nx=True, px=ttl_ms):
            return LockResult(LockState.ACQUIRED, token)
        return LockResult(LockState.BUSY)
    except Exception:
        logger.warning("acquire lock %s failed (redis unavailable)", name, exc_info=True)
        return LockResult(LockState.UNAVAILABLE)


def acquire_lock(name: str, ttl_ms: int) -> str | None:
    """兼容旧调用方：Redis 不可用时返回 bypass，仅 crawler 应继续使用。"""
    result = acquire_lock_result(name, ttl_ms)
    if result.state == LockState.ACQUIRED:
        return result.token
    if result.state == LockState.UNAVAILABLE:
        return _REDIS_BYPASS
    return None


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
