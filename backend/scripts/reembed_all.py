"""全量重嵌：切换 Embedding 模型/维度后，重派全部文章的 embedding 任务。

运行时切换 Embedding 服务后，存量向量仍是旧模型产物，与新向量比余弦会污染
事件聚类（管理面板 /admin/status 的 embedding_stale=true 即此状态）。
在容器内执行：python scripts/reembed_all.py
每篇成功后自动刷新指纹；全部完成告警自动消除。费用按价目表照常入账。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import new_session  # noqa: E402
from app.models.article import Article  # noqa: E402
from app.tasks.tasks import embedding_task  # noqa: E402


def main() -> None:
    db = new_session()
    try:
        ids = [row[0] for row in db.query(Article.id).all()]
    finally:
        db.close()
    print(f"re-embed all: dispatching {len(ids)} embedding_task (trigger=manual_repair)")
    for aid in ids:
        embedding_task.delay(aid, trigger="manual_repair")
    print("dispatched. 指纹将随每篇成功写入自动刷新为最新生效配置。")


if __name__ == "__main__":
    main()
