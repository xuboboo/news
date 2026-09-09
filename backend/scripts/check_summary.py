"""检查摘要中是否残留站点套话。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import new_session
from app.models.article import Article  # noqa: F401 确保 relationship 可解析
from app.models.summary import ArticleSummary


def main() -> None:
    db = new_session()
    try:
        total = db.query(ArticleSummary).count()
        bad_ids = []
        for s in db.query(ArticleSummary).all():
            if "线索投递" in (s.summary_short or ""):
                bad_ids.append(s.article_id)
        print(f"total={total} boilerplate_in_summary={len(bad_ids)} {bad_ids[:5]}")
        s = db.query(ArticleSummary).order_by(ArticleSummary.id.asc()).first()
        if s is not None:
            print("sample:", (s.summary_short or "")[:70])
    finally:
        db.close()


if __name__ == "__main__":
    main()
