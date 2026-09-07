"""爬虫常驻循环（docker-compose 的 crawler 服务入口）。"""
import logging
import time

from app.core.config import get_settings
from app.core.db import new_session
from app.crawler.ithome import ingest_all
from app.services.taskrun_service import record_task_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("crawler-loop")


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
                            logger.info("ingest result: %s", result)
                            # 为新文章派发 AI 处理任务
                            if result["new_ids"]:
                                from app.tasks.tasks import summary_task

                                for aid in result["new_ids"]:
                                    summary_task.delay(aid)
                                logger.info("dispatched %s summary tasks", len(result["new_ids"]))
                        finally:
                            db.close()
                finally:
                    release_lock(LOCK_CRAWLER)
        except Exception:
            logger.exception("crawl round failed")
        time.sleep(max(1, interval_min) * 60)


if __name__ == "__main__":
    main()
