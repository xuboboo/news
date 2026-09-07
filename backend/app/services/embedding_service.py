"""Embedding 服务：文章 → 向量写入 article.embedding（PRD 第 8 节）。

成功写入 API 向量后同步记录模型指纹（system_setting.embedding_fingerprint，
格式 "model|dim"）：管理面板据此检测"运行时切换了 Embedding 模型但存量向量
仍是旧模型"的漂移状态，提示运维执行 scripts/reembed_all.py 全量重嵌。
本地哈希回退（未配置 API）不刷新指纹——哈希向量与 API 向量不可比，不能覆盖记录。
"""
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
    article.embedding = embed(
        text,
        cfg,
        article_id=article_id,
        trigger=trigger,
    )
    article.ai_status = "embedded"
    # 仅 API 向量（配置齐全且本次调用走 API 成功）刷新指纹
    if cfg and all(cfg):
        _save_fingerprint(db, f"{cfg[2]}|{len(article.embedding)}")
    db.commit()
    db.refresh(article)
    return article
