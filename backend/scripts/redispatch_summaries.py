"""重新派发全部文章的摘要任务（重建摘要/向量）。"""
from app.core.db import new_session
from app.models.article import Article
from app.tasks.tasks import summary_task

db = new_session()
ids = [a.id for a in db.query(Article).all()]
print("redispatch summaries:", len(ids))
# 补数据重跑：账面记 manual_repair，与流水线调用区分
for i in ids:
    summary_task.delay(i, trigger="manual_repair")
