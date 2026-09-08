"""回填事件时间线：为 timeline 为空的事件从成员文章重建时间线。

背景：2026-09-09 审计发现 55 个事件 timeline 为空——事件只在「新增成员或摘要
脏数据」时才会重建时间线（event_service.run_event_clustering 第 4 步），
在此之前创建且成员未再变化的事件永远没有时间线，事件详情页缺时间线板块。

幂等：只处理 timeline 为空的事件，已有时间线的不动。只读成员表，不碰摘要/热度。

用法（容器内）：python scripts/backfill_event_timelines.py
"""
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.utils.text import truncate


def _tz_iso(t):
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.isoformat()


def main() -> None:
    db = SessionLocal()
    try:
        events = db.query(HotEvent).all()
        fixed = skipped = empty = 0
        for ev in events:
            if ev.timeline:
                skipped += 1
                continue
            members = [
                m
                for m in (
                    db.get(Article, l.article_id)
                    for l in db.query(EventArticle).filter_by(event_id=ev.id).all()
                )
                if m is not None
            ]
            if not members:
                empty += 1
                continue
            ev.timeline = [
                {
                    "time": _tz_iso(m.publish_time),
                    "text": truncate(m.summary.summary_short, 60) if m.summary else truncate(m.title, 60),
                    "source": m.source,
                }
                for m in sorted(members, key=lambda x: x.publish_time, reverse=True)
            ]
            fixed += 1
        db.commit()
        print(f"回填 {fixed} 个事件的时间线；已有时间线跳过 {skipped} 个；无成员事件 {empty} 个")
    finally:
        db.close()


if __name__ == "__main__":
    main()
