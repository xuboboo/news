"""Celery 任务（PRD 第 11 节）：summary_task / embedding_task / event_task / report_task / crawler_task / stats_task

粗粒度任务（crawler / event / report / stats）统一用 record_task_run 埋点，
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
    """采集任务：多来源采集（IT之家 / 快科技，见 crawler/multi.py）→ 入库 → 为新文章派发摘要任务。

    与常驻 crawler loop 双入口互斥：抢不到 Redis 锁说明另一入口正在采集，
    本轮直接跳过（记 success + detail，避免面板误报失败）。
    """
    from app.crawler.multi import format_detail, has_source_error, ingest_all_sources
    from app.utils.lock import LOCK_CRAWLER, LOCK_CRAWLER_TTL_MS, acquire_lock, release_lock

    if not acquire_lock(LOCK_CRAWLER, LOCK_CRAWLER_TTL_MS):
        with record_task_run("crawler", trigger=trigger) as run:
            run.detail = "skipped: another crawl is running"
        return {"skipped": True}
    try:
        with record_task_run("crawler", trigger=trigger) as run:
            db = new_session()
            try:
                result = ingest_all_sources(db)
                run.detail = format_detail(result)
                if has_source_error(result):
                    # 单来源失败不再静默记成功：任务降级为 degraded，
                    # 面板/告警可见；其余来源数据照常入库
                    run.status = "degraded"
                for article_id in result["new_ids"]:
                    summary_task.delay(article_id)
                # 孤儿自愈：补派历史滞留 pending（幂等，见 summary_service.repair_pending_summaries）
                from app.services.summary_service import repair_pending_summaries

                repaired = repair_pending_summaries(db)
                if repaired:
                    run.detail += f" ｜ 补派 pending {len(repaired)} 篇"
                return result
            finally:
                db.close()
    finally:
        release_lock(LOCK_CRAWLER)


@celery_app.task(name="stats_task")
def stats_task(trigger: str = "auto"):
    """互动数据轮询：评论数/点赞数按分层节奏更新文章（见 crawler/stats.py）。

    beat 每 15 分钟与管理面板手动触发双入口，lock:stats 互斥；
    system_setting.stats_poll_enabled 可在面板热停（关闭时不产生 task_run 记录）。
    """
    from app.crawler.stats import poll_due_articles
    from app.services import config_service
    from app.utils.lock import LOCK_STATS, LOCK_STATS_TTL_MS, acquire_lock, release_lock

    db = new_session()
    try:
        enabled = config_service.get_effective(db, "stats_poll_enabled")
    finally:
        db.close()
    if not enabled:
        logger.info("stats_task skipped: disabled by config")
        return {"skipped": True, "reason": "disabled"}

    if not acquire_lock(LOCK_STATS, LOCK_STATS_TTL_MS):
        with record_task_run("stats", trigger=trigger) as run:
            run.detail = "skipped: another stats poll is running"
        return {"skipped": True}
    try:
        with record_task_run("stats", trigger=trigger) as run:
            db = new_session()
            try:
                r = poll_due_articles(db)
                run.detail = "轮询 %d · 成功 %d · 失败 %d · 放弃 %d" % (
                    r["polled"], r["ok"], r["failed"], r["gave_up"],
                )
                if r.get("skipped_backoff"):
                    run.detail += " ｜ 域名限流跳过 %d 篇（保持到期下轮再试）" % r["skipped_backoff"]
                if r.get("deadline_hit"):
                    run.detail += " ｜ 触达轮次时长截止，剩余文章下轮再试"
                return r
            finally:
                db.close()
    finally:
        release_lock(LOCK_STATS)


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
    """事件聚合任务：聚类近期文章 → 事件间二阶段合并 → 重算热度。

    lock:event 与手动触发互斥：聚类/合并非原子读改写，并发会产生重复事件；
    单 worker 串行下是双保险，扩多 worker 后是正确性前提。
    """
    from app.utils.lock import LOCK_EVENT, LOCK_EVENT_TTL_MS, acquire_lock, release_lock

    if not acquire_lock(LOCK_EVENT, LOCK_EVENT_TTL_MS):
        with record_task_run("event", trigger=trigger) as run:
            run.detail = "skipped: another event clustering is running"
        return {"skipped": True}
    try:
        with record_task_run("event", trigger=trigger) as run:
            db = new_session()
            try:
                stats = run_event_clustering(db)
                recompute_event_scores(db)
                run.detail = str(stats)
                return stats
            finally:
                db.close()
    finally:
        release_lock(LOCK_EVENT)


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
            row, push_status = generate_daily_report(db, day, trigger=trigger)
            run.detail = "第 %d 期（%s）" % (row.issue_no, row.date.isoformat())
            if push_status:
                run.detail += " ｜ 推送：" + push_status
            return {"date": row.date.isoformat(), "issue_no": row.issue_no}
        finally:
            db.close()


@celery_app.task(name="alert_watch_task")
def alert_watch_task():
    """告警巡检（beat 每 5 分钟）：Redis/Worker/任务停摆/失败未恢复 → webhook 推送。

    不写 task_run：每 5 分钟一行会把任务执行记录表刷屏；结果只进日志。
    """
    from app.services.alert_service import watch

    db = new_session()
    try:
        result = watch(db)
        if result["problems"]:
            logger.info("alert_watch: %s notified=%s", result["problems"], result["notified"])
        return result
    finally:
        db.close()


@celery_app.task(name="auth_cleanup_task")
def auth_cleanup_task():
    """每周清理：物理删除过期/已吊销会话 + 超过保留期的审计日志 + 超保留期的任务运行记录
    + 超保留期的 AI 用量流水（ai_usage_retention_days，0 = 永久保留）。

    task_run 以每 10-15 分钟一行的频率增长（event/stats/crawler），不清理会无限膨胀；
    运维排查一般只看近期，保留 90 天足够。ai_usage_log 是财务流水、增长慢，默认留 1 年。
    """
    from app.auth import sessions as session_store
    from app.models.task_run import TaskRun
    from app.services import audit_service, config_service, usage_service

    db = new_session()
    try:
        # 自愈兜底：停用账号/停用租户的残留存活会话统一吊销（旁路停用/并发窗口），
        # 随后的 purge_expired 会把吊销行一并物理删除
        stale_revoked = session_store.revoke_stale_disabled_sessions(db)
        sessions_deleted = session_store.purge_expired(db)
        try:
            days = int(config_service.get_effective(db, "audit_retention_days"))
        except Exception:
            days = 180
        audits_deleted = audit_service.purge_older_than(db, days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        task_runs_deleted = (
            db.query(TaskRun).filter(TaskRun.started_at < cutoff).delete(synchronize_session=False)
        )
        try:
            usage_days = int(config_service.get_effective(db, "ai_usage_retention_days"))
        except Exception:
            usage_days = 365
        usage_deleted = usage_service.cleanup_expired_usage(db, usage_days)
        db.commit()
        logger.info(
            "auth_cleanup: stale_disabled_sessions=%s sessions=%s audit_logs=%s task_runs=%s usage_logs=%s",
            stale_revoked, sessions_deleted, audits_deleted, task_runs_deleted, usage_deleted,
        )
        return {
            "stale_disabled_sessions_revoked": stale_revoked,
            "sessions_deleted": sessions_deleted,
            "audit_deleted": audits_deleted,
            "task_runs_deleted": task_runs_deleted,
            "usage_logs_deleted": usage_deleted,
        }
    finally:
        db.close()
