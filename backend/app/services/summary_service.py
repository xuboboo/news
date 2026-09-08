"""摘要服务：文章 → article_summary 表（PRD 第 5 节流程的落库实现）。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.summary import summarize
from app.models.article import Article
from app.models.summary import ArticleSummary
from app.services import config_service


def generate_summary(db: Session, article_id: int, trigger: str = "auto") -> ArticleSummary:
    article = db.get(Article, article_id)
    if not article:
        raise ValueError(f"article {article_id} not found")

    existing = db.query(ArticleSummary).filter_by(article_id=article_id).first()
    # LLM 配置运行时可改（管理面板 / system_setting 表），每次调用读取生效值
    llm = config_service.effective_llm(db)
    # article_id / trigger 透传给用量记账（ai_usage_log）
    result = summarize(
        article.title, article.content or article.title, llm,
        article_id=article_id, trigger=trigger,
    )

    if existing:
        existing.summary_short = result.summary_short
        existing.summary_long = result.summary_long
        existing.key_points = result.key_points
        existing.tags = result.tags
        existing.model = result.model
        summary = existing
    else:
        summary = ArticleSummary(
            article_id=article_id,
            summary_short=result.summary_short,
            summary_long=result.summary_long,
            key_points=result.key_points,
            tags=result.tags,
            model=result.model,
        )
        db.add(summary)

    article.ai_status = "summarized"
    try:
        db.commit()
    except IntegrityError:
        # 并发派发同一文章（如重嵌脚本与新流水线同时跑）撞 uq_summary_article：
        # 回滚后改走更新分支，双任务均成功
        db.rollback()
        summary = db.query(ArticleSummary).filter_by(article_id=article_id).first()
        if summary is None:
            raise
        summary.summary_short = result.summary_short
        summary.summary_long = result.summary_long
        summary.key_points = result.key_points
        summary.tags = result.tags
        summary.model = result.model
        article.ai_status = "summarized"  # 回滚会撤销之前的修改，需重设
        db.commit()
    db.refresh(summary)
    return summary


def repair_pending_summaries(
    db: Session, older_than_minutes: int = 30, limit: int = 200
) -> list[int]:
    """孤儿自愈：找出长时间停留在 pending 的文章并补派摘要任务，返回补派 id 列表。

    场景：摘要任务的派发发生在采集入库之后——进程在两者之间中断（重启/崩溃）
    就会留下永不自处理的 pending 文章（2026-09-09 实测 28 篇滞留约 2 小时，
    直到 broker 因 worker 重连重投递才自愈）。generate_summary 为 upsert 语义，
    补派幂等；每轮采集触发一次，30 分钟阈值天然限频。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    ids = [
        row[0]
        for row in db.query(Article.id)
        .filter(Article.ai_status == "pending", Article.created_at <= cutoff)
        .limit(limit)
        .all()
    ]
    if ids:
        from app.tasks.tasks import summary_task  # 延迟导入避免循环依赖

        for aid in ids:
            summary_task.delay(aid)
    return ids
