"""存量数据修复（验收问题 1/2）：

1. 清洗 article.content 中的致谢行 / 广告声明；
2. 正文被清洗过的文章重生成摘要 + Embedding（保持语义一致）；
3. 重算所有事件总结（修复致谢摘要 + 「共有 N 篇」计数与实际成员数不一致）；
4. 重算事件热度并重建当日日报。

用法（容器内）：python scripts/repair_data.py
"""
import sys
from datetime import date as date_type
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import SessionLocal
from app.crawler.ithome import clean_content
from app.services.embedding_service import generate_embedding
from app.services.event_service import (
    _compose_impacts,
    _compose_summary,
    _summary_dirty,
    recompute_event_scores,
    run_event_clustering,
)
from app.services.report_service import generate_daily_report
from app.services.summary_service import generate_summary
from app.models.article import Article
from app.models.event import EventArticle, HotEvent

COUNT_RE = __import__("re").compile(r"共有 (\d+) 篇")


def main() -> None:
    db = SessionLocal()
    try:
        # 1) 清洗正文
        cleaned = 0
        for a in db.query(Article).all():
            new_text = clean_content(a.content or "")
            if new_text != (a.content or ""):
                a.content = new_text
                cleaned += 1
        db.commit()
        print(f"[1/5] 正文清洗完成：{cleaned} 篇被修改")

        # 2) 受影响文章重生成摘要 + 向量（摘要引擎不变时结果一致，此处保证摘要与正文对齐）
        # 补数据产生的 AI 调用记 manual_repair，与流水线调用在账面上区分
        for a in db.query(Article).all():
            generate_summary(db, a.id, trigger="manual_repair")
            generate_embedding(db, a.id, trigger="manual_repair")
        print("[2/5] 摘要与向量重生成完成")

        # 3) 事件总结修复：脏摘要或「共有 N 篇」计数不符的事件，用当前成员事实重算
        fixed_events = 0
        for ev in db.query(HotEvent).all():
            links = db.query(EventArticle).filter_by(event_id=ev.id).all()
            members = [db.get(Article, l.article_id) for l in links]
            members = [m for m in members if m]
            if not members:
                continue
            count_m = COUNT_RE.search(ev.summary or "")
            count_ok = (not count_m) or (int(count_m.group(1)) == len(members))
            if _summary_dirty(ev.summary) or not count_ok:
                ev.summary = _compose_summary(members)
                ev.impacts = _compose_impacts(members)
                fixed_events += 1
        db.commit()
        print(f"[3/5] 事件总结修复完成：{fixed_events} 个事件被重写")

        # 4) 全量聚类一遍（清理套话摘要的时间线 + 并入增量成员）并重算热度
        stats = run_event_clustering(db)
        recompute_event_scores(db)
        print(f"[4/5] 聚类完成：{stats}，事件热度已重算")

        # 5) 重建当日日报
        row = generate_daily_report(db, date_type.today())
        print(f"[5/5] 日报重建完成：{row.date.isoformat()} 第 {row.issue_no} 期")
    finally:
        db.close()


if __name__ == "__main__":
    main()
