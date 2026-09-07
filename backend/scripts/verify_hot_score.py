"""对拍：hot_score_expr（SQL 版）与 hot_score（Python 版）数值一致性。

在容器内/可连库环境运行：python scripts/verify_hot_score.py
抽样近期文章逐篇对比，误差超过 0.1 记 FAIL（两版本必须语义一致）。
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func  # noqa: E402

from app.algorithms.hot_score import hot_score, hot_score_expr  # noqa: E402
from app.core.db import new_session  # noqa: E402
from app.models.article import Article  # noqa: E402

TOLERANCE = 0.1
SAMPLE = 200


def main() -> int:
    db = new_session()
    try:
        ids = db.query(Article.id).order_by(Article.id.desc()).limit(1000).all()
        ids = [r[0] for r in ids]
        if not ids:
            print("SKIP: 库中无文章")
            return 0
        sample = random.sample(ids, min(SAMPLE, len(ids)))
        bad = 0
        for aid in sample:
            a = db.get(Article, aid)
            sql_val = db.query(hot_score_expr(Article)).filter(Article.id == aid).scalar()
            py_val = hot_score(a.views or 0, a.comments or 0, a.shares or 0, a.publish_time)
            # 两次取值间隔数毫秒，衰减差异可忽略
            if sql_val is None or abs(float(sql_val) - py_val) > TOLERANCE:
                bad += 1
                print(f"MISMATCH id={aid} sql={sql_val} py={py_val}")
        total = db.query(func.count(Article.id)).scalar()
        print(f"hot_score 对拍: sampled={len(sample)} mismatches={bad} (库内共 {total} 篇)")
        return 1 if bad else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
