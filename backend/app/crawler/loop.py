"""爬虫常驻循环（docker-compose 的 crawler 服务入口）。"""
import json
import logging
import time

from app.core.config import get_settings
from app.core.db import new_session
from app.crawler.multi import format_detail, has_source_error, ingest_all_sources
from app.services.taskrun_service import record_task_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("crawler-loop")

# 心跳文件：每轮结束写入时间戳与当轮间隔，供容器 healthcheck 判断循环悬挂
# （restart 只能救崩溃，救不了卡死在无超时网络调用上的进程）。
HEARTBEAT_FILE = "/tmp/crawler-loop-heartbeat"


def _write_heartbeat(interval_min: int) -> None:
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "interval_min": interval_min}, f)
    except OSError:
        pass  # 心跳写不进不影响采集（如只读 fs），healthcheck 会退化为 unhealthy 提示


def main():
    s = get_settings()
    logger.info("crawler loop started, interval=%s min", s.crawler_interval_minutes)
    while True:
        # 每轮动态读取生效间隔：管理面板改 crawler_interval_minutes 立即生效，无需重启
        db = new_session()
        interval_min = s.crawler_interval_minutes
        try:
            from app.services.config_service import get_effective

            interval_min = int(get_effective(db, "crawler_interval_minutes"))
        except Exception:
            pass
        finally:
            db.close()

        # 异常在 with 之外捕获：record_task_run 需要看到异常才能把本轮如实记为 failed，
        # 外层仅保证循环进程不死（与 celery crawler_task 行为一致）
        try:
            # 与手动触发的 crawler_task 双入口互斥：抢不到锁说明对方正在采集，本轮跳过
            from app.utils.lock import LOCK_CRAWLER, LOCK_CRAWLER_TTL_MS, acquire_lock, release_lock

            if not acquire_lock(LOCK_CRAWLER, LOCK_CRAWLER_TTL_MS):
                logger.info("another crawl is running, skip this round")
            else:
                try:
                    with record_task_run("crawler", trigger="auto") as run:
                        db = new_session()
                        try:
                            result = ingest_all_sources(db)
                            run.detail = format_detail(result)
                            if has_source_error(result):
                                # 单来源失败不再静默记成功：本轮降级为 degraded
                                run.status = "degraded"
                            logger.info("ingest result: %s", result)
                            # 为新文章派发 AI 处理任务
                            if result["new_ids"]:
                                from app.tasks.tasks import summary_task

                                for aid in result["new_ids"]:
                                    summary_task.delay(aid)
                                logger.info("dispatched %s summary tasks", len(result["new_ids"]))
                            # 孤儿自愈：补派历史滞留 pending（幂等，见 summary_service.repair_pending_summaries）
                            from app.services.summary_service import repair_pending_summaries

                            repaired = repair_pending_summaries(db)
                            if repaired:
                                logger.info("repaired %s stuck-pending articles", len(repaired))
                            # LLM 降级自愈：重派近期仍停留抽取式摘要的文章（LLM 恢复后补上）
                            from app.services.summary_service import repair_llm_fallback_summaries

                            fallback = repair_llm_fallback_summaries(db)
                            if fallback:
                                logger.info("re-summarized %s llm-fallback articles", len(fallback))
                            # 向量失败自愈：重派摘要完成但向量缺失的近期文章（重试耗尽后补上）
                            from app.services.embedding_service import repair_stuck_embeddings

                            stuck = repair_stuck_embeddings(db)
                            if stuck:
                                logger.info("re-dispatched %s stuck embeddings", len(stuck))
                        finally:
                            db.close()
                finally:
                    release_lock(LOCK_CRAWLER)
        except Exception:
            logger.exception("crawl round failed")
        _write_heartbeat(max(1, interval_min))
        time.sleep(max(1, interval_min) * 60)


if __name__ == "__main__":
    main()
