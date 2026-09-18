"""L-1（2026-09-18 审计整改）：Redis 互斥锁的持有者校验语义。

假 client 模拟 SET NX 与 compare-and-delete Lua：
- acquire 成功返回 token；被持有时返回 None；Redis 挂了返回哨兵（放行主链路）
- release 只删自己持有的锁；token 不匹配（超时被接管）不动别人的锁；
  哨兵/None 不发 Redis 命令
"""
from app.utils import lock as lock_mod


class FakeRedis:
    """最小 Redis 替身：只实现 lock.py 用到的 set/eval。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, px=None):  # noqa: ANN001, ARG002
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, token):  # noqa: ANN001
        assert "redis.call" in script and numkeys == 1
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


class DeadRedis:
    def set(self, *a, **kw):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    eval = set


def test_acquire_returns_token_and_release_deletes(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(lock_mod, "_client", lambda: fake)
    token = lock_mod.acquire_lock("t1", 1000)
    assert token and fake.store[f"lock:t1"] == token
    lock_mod.release_lock("t1", token)
    assert "lock:t1" not in fake.store


def test_acquire_held_returns_none(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(lock_mod, "_client", lambda: fake)
    assert lock_mod.acquire_lock("t2", 1000) is not None
    assert lock_mod.acquire_lock("t2", 1000) is None  # 已被持有


def test_release_token_mismatch_keeps_new_holder(monkeypatch):
    """A 超时后被 B 接管：A 收尾 release 不得删掉 B 的锁。"""
    fake = FakeRedis()
    monkeypatch.setattr(lock_mod, "_client", lambda: fake)
    token_a = lock_mod.acquire_lock("t3", 1000)
    fake.store["lock:t3"] = "someone-else-token"  # 模拟过期后 B 接管
    lock_mod.release_lock("t3", token_a)
    assert fake.store["lock:t3"] == "someone-else-token"


def test_redis_down_acquire_bypass_and_release_noop(monkeypatch):
    monkeypatch.setattr(lock_mod, "_client", lambda: DeadRedis())
    token = lock_mod.acquire_lock("t4", 1000)
    assert token  # truthy：Redis 故障不阻塞主链路
    lock_mod.release_lock("t4", token)  # 哨兵：不发命令、不抛错
    lock_mod.release_lock("t4", None)  # 未持锁 release 同样静默
