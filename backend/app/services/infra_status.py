"""基础设施探测与任务节奏心跳（admin /status、/health deep、告警巡检三方共用）。

设计要点：
- Redis 客户端模块级共享（此前 admin_status 每次轮询新建 client 且从不关闭，连接泄漏）。
- Celery inspect ping 超时 4s，空结果重试一次再判负（2s 超时在高负载下易抖动误报）。
- 任务节奏心跳：beat 是单点且 inspect.ping() 看不见它——beat 挂掉后 worker 全部在线、
  面板全绿，但定时任务全部停摆。用「最近一次运行距今是否超出预期节奏」来感知停摆。
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import redis as redis_lib
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.task_run import TaskRun

logger = logging.getLogger(__name__)

# ———— 模块级共享 Redis 客户端（线程安全：ConnectionPool 连接池复用） ————
_REDIS_POOL: dict[str, redis_lib.ConnectionPool] = {}
_REDIS_POOL_LOCK = threading.Lock()


def redis_client() -> redis_lib.Redis:
    """共享连接池上的 Redis 客户端；连接由池统一管理，调用方无需 close。"""
    url = get_settings().redis_url
    with _REDIS_POOL_LOCK:
        pool = _REDIS_POOL.get(url)
        if pool is None:
            pool = redis_lib.ConnectionPool.from_url(
                url, socket_connect_timeout=2, socket_timeout=2
            )
            _REDIS_POOL[url] = pool
    return redis_lib.Redis(connection_pool=pool)


def redis_ping(timeout: float = 2.0) -> tuple[bool, int | None]:
    """ping Redis，返回 (ok, latency_ms)；连接失败返回 (False, None)。"""
    try:
        client = redis_client()
        t0 = time.monotonic()
        ok = bool(client.ping())
        return ok, int((time.monotonic() - t0) * 1000)
    except Exception:
        return False, None


def celery_workers_ping(timeout: float = 4.0) -> tuple[list[str], str | None]:
    """广播 ping 全部 worker，返回 (在线名单, 错误)。空结果自动重试一次。"""
    from app.tasks.celery_app import celery_app

    last_err: str | None = None
    for attempt in range(2):
        try:
            pong = celery_app.control.inspect(timeout=timeout).ping()
            if isinstance(pong, dict) and pong:
                return sorted(pong.keys()), None
            last_err = None  # ping 正常返回但无人应答（broker 通、worker 死）
        except Exception as exc:
            last_err = str(exc)[:200]
        if attempt == 0:
            time.sleep(0.5)
    return [], last_err


# ———— 任务节奏心跳 ————

# 各粗粒度任务的调度节奏（分钟基数）与判停阈值 = 基数 × 倍数
_CADENCE: dict[str, dict] = {
    # beat 每 10 分钟调度 event_task；30 分钟没跑即视为调度停摆
    "event": {"label": "事件聚类", "base_minutes": 10, "multiplier": 3},
    # crawler 常驻循环按 crawler_interval_minutes 每轮一跑，间隔可被面板热调
    "crawler": {"label": "文章采集", "base_minutes": 45, "multiplier": 3},
    # beat 每 15 分钟调度 stats_task；面板热停（enabled=false）时不产生记录，调用方负责豁免
    "stats": {"label": "互动轮询", "base_minutes": 15, "multiplier": 3},
    # 日报每日 08:00（北京时间）；阈值 26h 容忍正常日内波动
    "report": {"label": "日报生成", "base_minutes": 26 * 60, "multiplier": 1},
}


def _threshold_minutes(name: str, interval_minutes: int | None) -> int:
    """判停阈值（分钟）；crawler 动态跟随采集间隔（间隔本身可被面板热调），下限 45 分钟。"""
    spec = _CADENCE[name]
    if name == "crawler":
        return max(spec["base_minutes"], (interval_minutes or 0) * spec["multiplier"])
    return spec["base_minutes"] * spec["multiplier"]


def _aware(dt: datetime) -> datetime:
    """SQLite（测试库）取回 naive datetime，Postgres 返回 aware——统一按 UTC 处理。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cadence_warnings(
    db: Session, interval_minutes: int | None = None, skip: set[str] | None = None
) -> list[dict]:
    """检查各任务最近一次运行是否超出节奏，返回停摆项列表。

    只有「曾经跑过、现在迟到了」才告警——全新部署从未运行过的任务不判停，
    避免刚起服时满屏红灯；skip 用于豁免被面板热停的任务（如 stats_poll_enabled=false）。
    """
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for name, spec in _CADENCE.items():
        if skip and name in skip:
            continue
        last = (
            db.query(TaskRun).filter(TaskRun.name == name).order_by(TaskRun.id.desc()).first()
        )
        if last is None or last.started_at is None:
            continue
        threshold = _threshold_minutes(name, interval_minutes)
        age = (now - _aware(last.started_at)).total_seconds() / 60
        if age > threshold:
            out.append(
                {
                    "name": name,
                    "label": spec["label"],
                    "last_run_at": last.started_at.isoformat(),
                    "age_minutes": round(age),
                    "threshold_minutes": threshold,
                }
            )
    return out


def crawler_age_minutes(db: Session) -> float | None:
    """最近一轮采集（含失败/跳过）距今多少分钟；从未运行返回 None。"""
    last = db.query(TaskRun).filter(TaskRun.name == "crawler").order_by(TaskRun.id.desc()).first()
    if last is None or last.started_at is None:
        return None
    return (datetime.now(timezone.utc) - _aware(last.started_at)).total_seconds() / 60


def deep_checks(db: Session, interval_minutes: int | None = None) -> dict:
    """/health?deep=1 用：DB 已由调用方检查，这里补 Redis/Celery/采集新鲜度。

    crawler 从未运行过（fresh=None）不判 degraded——刚部署首个周期未满属正常。
    """
    redis_ok, _ = redis_ping()
    workers, _err = celery_workers_ping(timeout=2)
    age = crawler_age_minutes(db)
    crawler_fresh: bool | None = None if age is None else age <= _threshold_minutes(
        "crawler", interval_minutes
    )
    checks = {"db": True, "redis": redis_ok, "celery": bool(workers), "crawler": crawler_fresh}
    failed = [k for k, v in checks.items() if v is False]
    return {"status": "degraded" if failed else "ok", "checks": checks, "failed": failed}
