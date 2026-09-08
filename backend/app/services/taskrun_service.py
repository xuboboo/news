"""任务执行记录服务：给 celery 任务与爬虫循环统一埋点。"""
import logging
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.core.db import new_session
from app.models.task_run import TaskRun

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


@contextmanager
def record_task_run(name: str, trigger: str = "auto"):
    """上下文管理器：进入即写 running 记录，结束更新成败与耗时。

    用法：
        with record_task_run("crawler", trigger="manual") as run:
            ...
            run.detail = "新增 12 篇"   # 可选：成功摘要

    任务内部异常会被记录为 failed 后原样抛出（不吞异常）。
    """
    db = new_session()
    # 僵尸清扫：进程中断会留下永远停在 running 的记录（restart 只能救进程，
    # 救不了已丢上下文的行，2026-09-09 实测一条 running 挂了 1.5h+）。
    # 超 6h 的 running 一律标记 stale，管理面板不再误判「任务仍在执行」。
    stale_count = (
        db.query(TaskRun)
        .filter(TaskRun.status == "running", TaskRun.started_at < _now() - timedelta(hours=6))
        .update(
            {
                "status": "stale",
                "finished_at": _now(),
                "detail": "进程中断未正常收尾（超 6h 自动标记）",
            },
            synchronize_session=False,
        )
    )
    if stale_count:
        logger.warning("marked %s stale task_run rows", stale_count)
    run = TaskRun(name=name, trigger=trigger, status="running")
    db.add(run)
    db.commit()
    start = time.monotonic()
    try:
        yield run
    except Exception as exc:
        try:
            err = "".join(traceback.format_exception_only(type(exc), exc)).strip() or repr(exc)
            run.status = "failed"
            run.finished_at = _now()
            run.duration_ms = int((time.monotonic() - start) * 1000)
            run.detail = err[:400]
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("failed to write task_run failure record")
        raise
    else:
        try:
            run.status = "success"
            run.finished_at = _now()
            run.duration_ms = int((time.monotonic() - start) * 1000)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("failed to write task_run success record")
    finally:
        db.close()
