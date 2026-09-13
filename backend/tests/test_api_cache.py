"""api_cache 单测：命中/未命中/TTL=0 禁用/Redis 故障静默降级。不依赖真实 Redis。

读接口响应缓存（services/api_cache.py）的故障语义是关键契约：Redis 不可达时
必须退化为直连 DB（返回 None / 不抛异常），缓存层故障不得影响接口可用性。
"""
import json
from types import SimpleNamespace

from app.services import api_cache


class _FakeRedis:
    """get/setex 最小桩：fail=True 时模拟 Redis 不可达。"""

    def __init__(self, fail: bool = False):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def setex(self, key, ttl, val):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = val
        self.ttls[key] = ttl


def test_roundtrip_hit_and_miss(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(api_cache, "redis_client", lambda: client)

    assert api_cache.cache_get_json("k") is None  # 未命中
    api_cache.cache_set_json("k", {"items": [1, 2], "total": 2})
    assert api_cache.cache_get_json("k") == {"items": [1, 2], "total": 2}
    assert 0 < client.ttls["k"] <= api_cache._ttl()


def test_stored_value_is_json_string():
    """落库为 JSON 字符串：其他语言/工具（redis-cli）读缓存不依赖 pickle。"""
    client = _FakeRedis()
    api_cache.redis_client = lambda: client  # 直接赋值，无需还原（函数局部）
    api_cache.cache_set_json("k", {"a": "中文"})
    assert json.loads(client.store["k"]) == {"a": "中文"}


def test_redis_failure_degrades_to_miss(monkeypatch):
    """Redis 不可达：get 返回 None、set 不抛——调用方直连 DB，接口不受影响。"""
    monkeypatch.setattr(api_cache, "redis_client", lambda: _FakeRedis(fail=True))
    assert api_cache.cache_get_json("k") is None
    api_cache.cache_set_json("k", {"a": 1})  # 不应抛异常


def test_ttl_zero_disables_cache(monkeypatch):
    """API_CACHE_TTL_SECONDS=0（回滚开关）：读写全部短路，不触 Redis。"""
    monkeypatch.setattr(
        api_cache, "get_settings",
        lambda: SimpleNamespace(api_cache_ttl_seconds=0),
    )
    called = {"set": 0}

    def _boom_get(key):
        raise AssertionError("ttl=0 时不得访问 Redis")

    def _boom_set(key, ttl, val):
        called["set"] += 1
        raise AssertionError("ttl=0 时不得访问 Redis")

    client = SimpleNamespace(get=_boom_get, setex=_boom_set)
    monkeypatch.setattr(api_cache, "redis_client", lambda: client)
    assert api_cache.cache_get_json("k") is None
    api_cache.cache_set_json("k", {"a": 1})
    assert called["set"] == 0
