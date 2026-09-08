"""一次性修复：用边界感知截断重算存量事件的 impacts。

背景：_compose_impacts 旧版用 truncate(key_points[0], 64) 硬截断，存量事件里
存着"微软高管承诺将会基…"这类半字截断；且本地（无 LLM）模式下事件更新分支
不刷新 impacts，坏数据不会自愈。本脚本按当前 _compose_impacts 逻辑全量重算，
幂等可重跑（结果相同则不写）。

用法（容器内）：python scripts/repair_impacts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.services.event_service import _compose_impacts


def main() -> None:
    db = SessionLocal()
    try:
        events = db.query(HotEvent).all()
        changed = 0
        for ev in events:
            members = (
                db.query(Article)
                .join(EventArticle, EventArticle.article_id == Article.id)
                .filter(EventArticle.event_id == ev.id)
                .order_by(Article.publish_time)
                .all()
            )
            if not members:
                continue
            new_impacts = _compose_impacts(members)
            if new_impacts and new_impacts != list(ev.impacts or []):
                ev.impacts = new_impacts
                changed += 1
        db.commit()
        print(f"events={len(events)} impacts_updated={changed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
