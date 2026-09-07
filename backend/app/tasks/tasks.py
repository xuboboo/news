"""Celery 任务（PRD 第 11 节）：summary_task / embedding_task / event_task / report_task / crawler_task

粗粒度任务（crawler / event / report）统一用 record_task_run 埋点，
供管理面板"任务执行记录"展示；逐篇 summary/embedding 不记录。
"""
import logging
from datetime import datetime, timedelta, timezone

from app.ai.embedding import EmbeddingError
from app.core.db import new_session
from app.services.embedding_service import generate_embedding
from app.services.event_service import recompute_event_scores, run_event_clustering
from app.services.report_service import generate_daily_report
from app.services.summary_service import generate_summary
from app.services.taskrun_service import record_task_run
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="crawler_task")
def crawler_task(trigger: str = "auto"):
    """采集任务：抓取 RSS → 入库 → 为新文章派发摘要任务。

    与常驻 crawler loop 双入口互斥：抢不到 Redis 锁说明另一入口正在采集，
    本轮直接跳过（记 success + detail，避免面板误报失败）。
    """
    from app.crawler.ithome import ingest_all
    from app.utils.lock import LOCK_CRAWLER, LOCK_CRAWLER_TTL_MS, acquire_lock, release_lock

    if not acquire_lock(LOCK_CRAWLER, LOCK_CRAWLER_TTL_MS):
        with record_task_run("crawler", trigger=trigger) as run:
            run.detail = "skipped: another crawl is running"
        return {"skipped": True}
    try:
        with record_task_run("crawler", trigger=trigger) as run:
            db = new_session()
            try:
                result = ingest_all(db)
                per_channel = result.get("per_channel") or {}
                extra = ""
                if per_channel:
                    extra = " ｜ 新增分布：" + " / ".join(
                        f"{ch} {n}" for ch, n in per_channel.items()
                    )
                run.detail = "新增 %d 篇 · 扫描 %d · 跳过 %d · 频道纠偏 %d%s" % (
                    len(result["new_ids"]), result.get("scanned", 0),
                    result.get("skipped", 0), result.get("rechanneled", 0),
                    extra,
                )
                for article_id in result["new_ids"]:
                    summary_task.delay(article_id)
                return result
            finally:
                db.close()
    finally:
        release_lock(LOCK_CRAWLER)


@celery_app.task(name="summary_task")
def summary_task(article_id: int, trigger: str = "auto"):
    """AI 摘要任务：文章入库 → 队列 → AI Worker → 模型调用 → 结果保存。

    trigger 透传给用量记账（auto / manual_repair）。
    """
    db = new_session()
    try:
        s = generate_summary(db, article_id, trigger=trigger)
        embedding_task.delay(article_id, trigger=trigger)
        return {"article_id": article_id, "model": s.model}
    except Exception as exc:  # 任务失败不阻塞流水线
        logger.exception("summary_task failed: %s", exc)
        raise
    finally:
        db.close()


@celery_app.task(
    name="embedding_task",
    # 严格模式下 API 失败/维度不符抛 EmbeddingError：指数退避自动重试（30s→10min，最多 5 次）
    autoretry_for=(EmbeddingError,),
    retry_backoff=30,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def embedding_task(article_id: int, trigger: str = "auto"):
    """向量任务：生成 Embedding 并写入 pgvector（trigger 透传给用量记账）。"""
    db = new_session()
    try:
        generate_embedding(db, article_id, trigger=trigger)
        return {"article_id": article_id, "embedded": True}
    except EmbeddingError:
        raise  # 已由 embed() 记账并打日志，重试交给 Celery
    except Exception:
        logger.exception("embedding_task failed: %s", article_id)
        raise
    finally:
        db.close()


@celery_app.task(name="event_task")
def event_task(trigger: str = "auto"):
    """事件聚合任务：聚类近期文章 → 增量更新事件库 → 重算热度。"""
    with record_task_run("event", trigger=trigger) as run:
        db = new_session()
        try:
            stats = run_event_clustering(db)
            recompute_event_scores(db)
            run.detail = str(stats)
            return stats
        finally:
            db.close()


@celery_app.task(name="report_task")
def report_task(date_str: str | None = None, trigger: str = "auto"):
    """日报任务：每日 08:00（北京时间）自动生成「昨日（UTC）」报告。

    触发时刻为 UTC 00:00，若取"当天"则统计窗口刚开启、内容必然为空，
    因此自动口径固定为前一个 UTC 自然日；指定 date_str 可补生成任意一天。
    """
    with record_task_run("report", trigger=trigger) as run:
        db = new_session()
        try:
            if date_str:
                day = datetime.fromisoformat(date_str).date()
            else:
                day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
            row = generate_daily_report(db, day)
            run.detail = "第 %d 期（%s）" % (row.issue_no, row.date.isoformat())
            return {"date": row.date.isoformat(), "issue_no": row.issue_no}
        finally:
            db.close()
