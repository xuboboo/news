"""检查摘要中是否残留站点套话。"""
from app.core.db import new_session
from app.models.article import Article  # noqa: F401 确保 relationship 可解析
from app.models.summary import ArticleSummary

db = new_session()
total = db.query(ArticleSummary).count()
bad_ids = []
for s in db.query(ArticleSummary).all():
    if "线索投递" in (s.summary_short or ""):
        bad_ids.append(s.article_id)
print(f"total={total} boilerplate_in_summary={len(bad_ids)} {bad_ids[:5]}")
s = db.query(ArticleSummary).order_by(ArticleSummary.id.asc()).first()
print("sample:", s.summary_short[:70])
