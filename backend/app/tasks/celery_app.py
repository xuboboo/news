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
    # event_task 内逐事件串行调 LLM（每个最长 60s），放宽到 4h。
    # 官方要求 broker 与 result backend（同为 Redis）两侧设同一值，否则出现
    # 整点重投/结果消失类问题（celery#7651）
    broker_transport_options={"visibility_timeout": 4 * 3600},
    result_backend_transport_options={"visibility_timeout": 4 * 3600},
    # 硬上限 1h：卡死任务（LLM 悬挂/死锁）不再永久占住 worker 槽位，
    # 也保证任务在 4h visibility_timeout 内结束，不会因超时重投而重复执行
    task_soft_time_limit=55 * 60,
    task_time_limit=3600,
    # 路由：AI 处理走 ai 队列（由 ai-worker 消费），其余走 default
    task_routes={
        "summary_task": {"queue": "ai"},
        "embedding_task": {"queue": "ai"},
        "article_gen_task": {"queue": "ai"},
        "site_article_gen_task": {"queue": "ai"},
        "article_gen_failure_confirm_task": {"queue": "ai"},
        "article_draft_queue_recovery_task": {"queue": "default"},
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
        # 北京时间 08:00：生成「今日早报」发布日期刊——统计窗口为「昨夜今晨」
        # （昨日 00:00 → 发布日 08:00），与生成时刻恰合，每篇报道归属唯一一期
        "daily-report-0800": {
            "task": "report_task",
            "schedule": crontab(hour=8, minute=0),
        },
        # 早报补偿（每小时 :45）：本地 PC 关机/休眠/引擎停摆错过 08:00 时，
        # 自动补生成缺失期（历史回看 3 天 + 今天 08:00 后）；补算不推送，
        # 与 08:00 主调度靠 advisory lock 幂等，见 report_catchup_task
        "report-catchup-hourly": {
            "task": "report_catchup_task",
            "schedule": crontab(minute=45),
        },
        # 告警巡检：Redis/Worker/任务停摆/失败未恢复 → webhook 推送（面板可配 URL，留空关闭）
        "alert-watch-every-5min": {
            "task": "alert_watch_task",
            "schedule": crontab(minute="*/5"),
        },
        "article-draft-queue-recovery-every-10min": {
            "task": "article_draft_queue_recovery_task",
            "schedule": crontab(minute="*/10"),
        },
        # 每周一 03:30（北京时间）：清理过期会话与超保留期审计日志
        "auth-cleanup-weekly": {
            "task": "auth_cleanup_task",
            "schedule": crontab(hour=3, minute=30, day_of_week=1),
        },
    },
)
