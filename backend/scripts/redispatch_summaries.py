"""重新派发全部文章的摘要任务（重建摘要/向量）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import new_session
from app.models.article import Article
from app.tasks.tasks import summary_task


def main() -> None:
    db = new_session()
    try:
        ids = [a.id for a in db.query(Article).all()]
    finally:
        db.close()
    print("redispatch summaries:", len(ids))
    # 补数据重跑：账面记 manual_repair，与流水线调用区分
    for i in ids:
        summary_task.delay(i, trigger="manual_repair")


if __name__ == "__main__":
    main()
