"""登录 IP 限速：固定窗口计数，补账号锁定之外的另一半暴力破解防线（OWASP A07）。

账号级锁定只防"单账号猜爆"：攻击者对每个邮箱试 4 次可长期密码喷洒不触发，
且可反复锁死他人账号（定向 DoS）。IP 级窗口限制同时抬高两类攻击的成本；
NAT 后的正常用户由「登录成功即清零计数」兜底。

存储：优先 Redis（compose 内多进程共享），连接失败退化为进程内字典
（单 worker 部署下语义一致；本机裸跑/测试环境无 Redis 时走此路径）。
阈值与窗口走管理面板热调（login_ip_throttle_max / login_ip_throttle_window_seconds）。
"""
import threading
import time

import redis as redis_lib

from app.core.config import get_settings

_KEY_PREFIX = "login_rl:"

# 进程内回退存储：key -> (计数, 窗口截止 epoch 秒)
_INPROC: dict[str, tuple[int, int]] = {}
_INPROC_LOCK = threading.Lock()


def _redis_client() -> redis_lib.Redis | None:
    """惰性建连：Redis 不可达返回 None（连接失败不缓存，每次登录重试一次）。"""
    try:
        client = redis_lib.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        client.ping()
        return client
    except Exception:
        return None


def register_attempt(ip: str, *, max_attempts: int, window_seconds: int) -> tuple[bool, int]:
    """记一次登录尝试。返回 (是否放行, 拒绝时建议等待的秒数)。"""
    if not ip or max_attempts <= 0 or window_seconds <= 0:
        return True, 0
    key = _KEY_PREFIX + ip
    client = _redis_client()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.incr(key)
            # NX：只在键新建时定窗口，避免每次尝试顺延窗口变成滑动窗口
            pipe.expire(key, int(window_seconds), nx=True)
            count, _ = pipe.execute()
            if int(count) > max_attempts:
                ttl = int(client.ttl(key) or window_seconds)
                return False, max(1, ttl)
            return True, 0
        except Exception:
            pass  # Redis 抖动 → 进程内兜底，限速语义不变
    return _inproc_register(key, max_attempts, window_seconds)


def reset(ip: str) -> None:
    """登录成功后清除该 IP 计数（避免 NAT 用户被同出口 IP 的他人失败连坐）。"""
    if not ip:
        return
    key = _KEY_PREFIX + ip
    client = _redis_client()
    if client is not None:
        try:
            client.delete(key)
            return
        except Exception:
            pass
    with _INPROC_LOCK:
        _INPROC.pop(key, None)


def _inproc_register(key: str, max_attempts: int, window_seconds: int) -> tuple[bool, int]:
    now = int(time.time())
    with _INPROC_LOCK:
        # 回退存储没有过期回收，超阈值时顺手清一遍已过期条目防无界增长
        if len(_INPROC) > 50_000:
            expired = [k for k, (_, end) in _INPROC.items() if now >= end]
            for k in expired:
                _INPROC.pop(k, None)
        count, window_end = _INPROC.get(key, (0, 0))
        if now >= window_end:
            count, window_end = 0, now + int(window_seconds)
        count += 1
        _INPROC[key] = (count, window_end)
        if count > max_attempts:
            return False, max(1, window_end - now)
        return True, 0
