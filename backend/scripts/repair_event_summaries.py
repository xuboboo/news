"""一次性修复：兜底事件总结的引子与事件展示标题对齐。

背景：_compose_summary 旧版引子「围绕「X」」取成员首篇标题，而事件标题经
elect_title 选举可能与成员首篇不同，存量事件出现标题与总结各说各话；
本地（无 LLM）模式下事件只有成员变动才重建总结，坏数据不会自愈。
本脚本按当前 _compose_summary 逻辑（锚定 ev.title）全量重算兜底总结，
幂等可重跑（结果相同则不写）。已配置 LLM 的事件总结不在此处理——
那是模型产出而非拼装模板，重算会把它覆盖成拼装文本。

用法（容器内）：python scripts/repair_event_summaries.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.services.event_service import _compose_summary


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
            old_form = _compose_summary(members)  # 旧逻辑（锚点=成员首篇）
            new_summary = _compose_summary(members, anchor_title=ev.title)
            # 只改写「恰为旧模板形态」的行：LLM 产出/已修过的行不会被误覆盖
            if ev.summary and ev.summary == old_form and new_summary != old_form:
                ev.summary = new_summary
                changed += 1
        db.commit()
        print(f"events={len(events)} summaries_updated={changed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
