"""公开探针端点：不需要登录会话（基础设施健康检查）。

全站门禁的例外清单——新增公开端点必须放这里并同步 audit_auth.py 断言。

?deep=1 供外部监控（Uptime Kuma / compose healthcheck / 告警巡检）做深度探测：
追加 Redis / Celery worker / 采集新鲜度检查。只回布尔与总体状态，不暴露主机名、
版本、水位等内部细节；deep 探测含 Celery 广播 ping，建议监控间隔 ≥ 60s。
"""
from datetime import datetime, timezone
import time

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.article import Article

router = APIRouter(prefix="/api/v2")

# deep 探测含 Celery 广播 ping（无鉴权端点），15s 缓存防被刷拖累 broker
_DEEP_CACHE: dict = {"t": 0.0, "data": None}
_DEEP_TTL_SECONDS = 15.0


@router.get("/health")
def health(deep: bool = False, db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.query(func.count(Article.id)).scalar()
    except Exception:
        db_ok = False
    if not deep:
        return {
            "status": "ok" if db_ok else "degraded",
            "db": db_ok,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    deep_result = None
    if time.monotonic() - _DEEP_CACHE["t"] < _DEEP_TTL_SECONDS:
        deep_result = _DEEP_CACHE["data"]
    if deep_result is None:
        from app.services import config_service
        from app.services.infra_status import deep_checks

        try:
            interval = config_service.get_effective(db, "crawler_interval_minutes")
        except Exception:
            interval = None
        try:
            deep_result = deep_checks(db, interval)
            _DEEP_CACHE["t"] = time.monotonic()
            _DEEP_CACHE["data"] = deep_result
        except Exception:
            # DB 故障时 deep_checks 内的 TaskRun 查询会抛错：降级为结构化 degraded
            # 而非 500（redis/celery 两项仍有效，crawler 无法判定置 None 不参与判负）
            deep_result = {"status": "degraded", "checks": {"crawler": None}, "failed": []}

    # db 的真实结果放后面覆盖：deep_checks 内部对 db 恒为 True（它查不了调用方的检查）
    checks = {**deep_result["checks"], "db": db_ok}
    failed = [k for k, v in checks.items() if v is False]
    return {
        "status": "degraded" if failed else "ok",
        "failed": failed,
        "checks": checks,
        "time": datetime.now(timezone.utc).isoformat(),
    }
