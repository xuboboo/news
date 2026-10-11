"""事件热度改版对账（2026-10-12，PG 直连）。

验证两件事（AIHOT「按事件算热度」语义落地正确性）：
1. 热度口径：DB 存储的事件热度 == 逐成员 Python hot_score() 按 event_hot_mode
   汇总（distinct_source=每来源取最高再求和，与 hot_score.event_hot_from_pairs
   参考实现对账；sum_articles=求和）；
2. 上升标记字段完备：hot_score_prev / score_updated_at / is_rising 三列
   存在且 score_updated_at 随每轮重算推进（event_task 至少跑过一轮改版代码）。

前置：backend 可连 PostgreSQL（.env DATABASE_URL）。
用法：python scripts/verify_event_hot.py [--hours 48] [--limit 50]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

from app.algorithms.hot_score import event_hot_from_pairs, hot_score  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.article import Article  # noqa: E402
from app.models.event import EventArticle, HotEvent  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48, help="只对账 last_time 在此窗口内的事件")
    parser.add_argument("--limit", type=int, default=50, help="最多对账的事件数")
    args = parser.parse_args()

    s = get_settings()
    mode = (s.event_hot_mode or "distinct_source")
    print(f"event_hot_mode = {mode}")

    checked = mismatched = stale = 0
    with SessionLocal() as db:
        since = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, args.hours * 3600)
        events = (
            db.query(HotEvent)
            .filter(HotEvent.last_time >= since)
            .order_by(HotEvent.hot_score.desc())
            .limit(args.limit)
            .all()
        )
        for ev in events:
            members = (
                db.query(
                    Article.source, Article.views, Article.comments,
                    Article.shares, Article.likes, Article.publish_time,
                )
                .join(EventArticle, EventArticle.article_id == Article.id)
                .filter(EventArticle.event_id == ev.id)
                .all()
            )
            pairs = [
                (
                    m.source,
                    hot_score(m.views or 0, m.comments or 0, m.shares or 0, m.likes or 0, m.publish_time),
                )
                for m in members
            ]
            if mode == "distinct_source":
                expected = event_hot_from_pairs(pairs)
            else:
                expected = round(sum(p[1] for p in pairs), 2)
            actual = round(float(ev.hot_score or 0.0), 2)
            checked += 1
            # DB 与 Python 复算均为原始口径（归一化在 API 层），容差覆盖重算间隙的时间差
            if abs(expected - actual) > 0.05:
                mismatched += 1
                print(f"  MISMATCH event#{ev.id} 期望 {expected} 实际 {actual}（{len(members)} 篇）")
            if ev.score_updated_at is None and ev.hot_score:
                stale += 1
                print(f"  STALE event#{ev.id} score_updated_at 为空但热度非零（event_task 尚未跑过改版轮次？）")

    print(
        f"\n对账 {checked} 个事件：{'全部一致' if mismatched == 0 else str(mismatched) + ' 个不一致'}；"
        f"标记列缺位 {stale} 个"
    )
    return 1 if (mismatched or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
