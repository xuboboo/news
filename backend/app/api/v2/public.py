"""公开探针端点：不需要登录会话（基础设施健康检查）。

全站门禁的例外清单——新增公开端点必须放这里并同步 audit_auth.py 断言。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.article import Article

router = APIRouter(prefix="/api/v2")


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.query(func.count(Article.id)).scalar()
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "time": datetime.now(timezone.utc).isoformat()}
