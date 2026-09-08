"""智能时代频道存量清理（2026-09 口径切换一次性迁移）。

背景：智能时代频道曾误用 /tags/AI/ 与 /tags/ChatGPT/ 标签页采集，与官方
「智能时代」子站 next.ithome.com（2026-09 起的唯一采集源）内容对不上。
article 表未记录来源页面，无法精确区分污染篇目，故整频道重置：
- 删除 channel='智能时代' 的全部文章（摘要随 ORM 级联删除，
  事件关联由 FK CASCADE 清理，收尾时再删除成员为 0 的热点事件）；
- 新文章由切换后的爬虫在下一轮采集从官方子站重新入库。

用法（容器内）：python scripts/migrate_znsd_source.py [--dry-run]
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func

from app.core.db import SessionLocal
from app.models.article import Article
from app.models.event import EventArticle, HotEvent

CHANNEL = "智能时代"


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    db = SessionLocal()
    try:
        rows = db.query(Article).filter(Article.channel == CHANNEL).all()
        before = Counter(a.channel for a in db.query(Article).all())
        print(f"[1/3] {CHANNEL} 频道存量 {len(rows)} 篇{('（dry-run，不实际删除）' if dry_run else '')}")
        for a in rows:
            print(f"  - #{a.id} {a.publish_time:%m-%d %H:%M} {a.title[:46]}")

        if dry_run:
            print("dry-run 结束，未做任何修改。")
            return

        for a in rows:
            db.delete(a)
        db.commit()
        print(f"[2/3] 已删除文章 {len(rows)} 篇（摘要级联删除、事件关联 FK CASCADE）")

        counts = dict(
            db.query(EventArticle.event_id, func.count(EventArticle.id))
            .group_by(EventArticle.event_id)
            .all()
        )
        empty_ids = [ev.id for ev in db.query(HotEvent).all() if counts.get(ev.id, 0) == 0]
        if empty_ids:
            db.query(HotEvent).filter(HotEvent.id.in_(empty_ids)).delete(synchronize_session=False)
            db.commit()
        print(f"[3/3] 清理无成员热点事件 {len(empty_ids)} 个")

        after_rows = db.query(Article.channel, func.count(Article.id)).group_by(Article.channel).all()
        after = Counter(dict(after_rows))
        print("各频道数量（迁移前 → 迁移后）：")
        for ch in sorted(set(before) | set(after)):
            print(f"  {ch}: {before.get(ch, 0)} → {after.get(ch, 0)}")
        print("完成：等待下一轮采集（crawler 15min 周期或管理面板手动触发）从 next.ithome.com 重新入库。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
