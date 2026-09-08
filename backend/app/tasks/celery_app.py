"""Celery 应用（PRD 第 11 节）：默认队列 + ai 队列 + 定时调度。"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "hjradar",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    # acks_late 下，任务执行超过 visibility_timeout 会被 broker 视为未消费而重复投递；
    # event_task 内逐事件串行调 LLM（每个最长 60s），放宽到 4h
    broker_transport_options={"visibility_timeout": 4 * 3600},
    # 路由：AI 处理走 ai 队列（由 ai-worker 消费），其余走 default
    task_routes={
        "summary_task": {"queue": "ai"},
        "embedding_task": {"queue": "ai"},
    },
        # 定时任务（文章采集由常驻 crawler 服务循环执行，此处不再重复调度）
        beat_schedule={
            # 互动数据轮询（评论/点赞）：独立于采集循环，与 stats_task 面板手动触发共用 lock:stats
            "stats-poll-every-15min": {
                "task": "stats_task",
                "schedule": crontab(minute="*/15"),
            },
            "event-cluster-every-10min": {
                "task": "event_task",
                "schedule": crontab(minute="*/10"),
            },
            # 北京时间 08:00 = UTC 00:00，生成「昨日（UTC）」报告，避免当日窗口刚开启取到空数据
            "daily-report-0800": {
                "task": "report_task",
                "schedule": crontab(hour=8, minute=0),
            },
            # 每周一 03:30（北京时间）：清理过期会话与超保留期审计日志
            "auth-cleanup-weekly": {
                "task": "auth_cleanup_task",
                "schedule": crontab(hour=3, minute=30, day_of_week=1),
            },
        },
)
