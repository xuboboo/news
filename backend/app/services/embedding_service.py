"""Embedding 服务：文章 → 向量写入 article.embedding（PRD 第 8 节）。

成功写入 API 向量后同步记录模型指纹（system_setting.embedding_fingerprint，
格式 "model|dim"）：管理面板据此检测"运行时切换了 Embedding 模型但存量向量
仍是旧模型"的漂移状态，提示运维执行 scripts/reembed_all.py 全量重嵌。
本地哈希回退（未配置 API）不刷新指纹——哈希向量与 API 向量不可比，不能覆盖记录。
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.ai.embedding import embed
from app.models.article import Article
from app.models.setting import SystemSetting
from app.services import config_service

EMBEDDING_FINGERPRINT_KEY = "embedding_fingerprint"


def get_embedding_fingerprint(db: Session) -> str | None:
    """上次成功写入向量时的模型指纹（"model|dim"）；从未写过为 None。"""
    row = db.get(SystemSetting, EMBEDDING_FINGERPRINT_KEY)
    return row.value if row else None


def _save_fingerprint(db: Session, fingerprint: str) -> None:
    row = db.get(SystemSetting, EMBEDDING_FINGERPRINT_KEY)
    if row is None:
        db.add(SystemSetting(key=EMBEDDING_FINGERPRINT_KEY, value=fingerprint))
    else:
        row.value = fingerprint


def generate_embedding(db: Session, article_id: int, trigger: str = "auto") -> Article:
    article = db.get(Article, article_id)
    if not article:
        raise ValueError(f"article {article_id} not found")

    # 标题 + 正文前段联合编码：标题信息为主，正文补充语义
    text = f"{article.title}\n{(article.content or '')[:600]}"
    # Embedding 配置运行时可改（管理面板 / system_setting 表）
    # article_id / trigger 透传给用量记账（ai_usage_log）
    cfg = config_service.effective_embedding(db)
    vec_source: dict = {}
    article.embedding = embed(
        text,
        cfg,
        article_id=article_id,
        trigger=trigger,
        source_out=vec_source,
    )
    article.ai_status = "embedded"
    # 仅 API 成功向量刷新指纹（"model|dim"）。非 strict 模式下 API 失败会静默回退
    # 本地哈希向量——哈希向量与 API 向量不可比，把指纹记成 API 模型会让漂移检测失真
    if cfg and all(cfg) and vec_source.get("api"):
        _save_fingerprint(db, f"{cfg[2]}|{len(article.embedding)}")
    db.commit()
    db.refresh(article)
    return article


def repair_stuck_embeddings(
    db: Session, older_than_minutes: int = 30, within_hours: int = 24, limit: int = 10
) -> list[int]:
    """向量失败自愈：重派「摘要已完成但向量缺失」的近期文章，返回补派 id 列表。

    场景：strict 模式（默认）下 Embedding API 失败由任务层指数退避重试 5 次，
    全部耗尽后文章永久停留在 summarized 且无向量——事件聚类按向量相似度分组，
    无向量文章从此不被聚合，且无任何机制补上（2026-09-12 全面审计识别，
    与摘要/事件降级同类）。本函数随每轮采集触发：只回看 within_hours 内、
    滞留超过 older_than_minutes（给任务内重试链留足时间，避免与退避中的
    任务重复）的文章，单轮限量 limit；embedding 为 upsert 语义，补派幂等，
    成功后 ai_status 变为 embedded 不再命中本查询。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    matured = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    ids = [
        row[0]
        for row in (
            db.query(Article.id)
            .filter(
                Article.created_at >= cutoff,
                Article.created_at <= matured,
                Article.ai_status == "summarized",
                Article.embedding.is_(None),
            )
            .order_by(Article.id.desc())
            .limit(limit)
            .all()
        )
    ]
    if ids:
        from app.tasks.tasks import embedding_task  # 延迟导入避免循环依赖

        for aid in ids:
            embedding_task.delay(aid, trigger="manual_repair")
    return ids
