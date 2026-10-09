"""文章草稿模型：「选题 → AI 生成 → 编辑 → 复制发布」链路的编辑工作产物。

草稿全员共享（与资讯数据同口径，租户只隔离用户与权限），created_by 仅作追溯。
不设源外键——源文章/事件可能被清理，草稿须独立存活；source_title 冗余快照
保证列表展示与溯源不依赖源行。
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class ArticleDraft(Base):
    __tablename__ = "article_draft"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel = Column(String(16), nullable=False, default="wechat", index=True)  # wechat / site；旧稿默认公众号
    seo_keywords = Column(String(160), nullable=False, default="")  # CMS 页面关键字，仅后台字段
    site_tags = Column(String(120), nullable=False, default="")
    cover_guidance = Column(String(240), nullable=False, default="")  # 配图建议，不是图片文件
    software_id = Column(Integer, nullable=False, default=0)  # 仅人工关联，0=不关联
    is_pinned = Column(Boolean, nullable=False, default=False)
    is_recommended = Column(Boolean, nullable=False, default=False)
    is_headline = Column(Boolean, nullable=False, default=False)
    publish_at = Column(DateTime(timezone=True), nullable=True)  # 编辑拟定时间，不等于实际发布时间
    status = Column(String(16), nullable=False, default="generating", index=True)  # generating / ready / failed
    source_type = Column(String(16), nullable=False)               # event / article
    source_id = Column(Integer, nullable=False, index=True)        # 不设 FK：源可被清理，草稿独立存活
    source_title = Column(String(512), nullable=False, default="")  # 源标题快照（列表展示/溯源）
    tier = Column(String(16), nullable=False, default="brief")     # brief=快讯版 / depth=深度版
    title = Column(String(512), default="")                        # 公众号≤30字；网站稿≤80字
    alt_titles = Column(JSON().with_variant(JSONB, "postgresql"), default=list)  # 备选标题
    digest = Column(String(200), default="")                       # 公众号≤120字；网站简介≤160字
    content_md = Column(Text, default="")                          # Markdown 正文
    source_refs = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)  # 生成时的来源快照
    model = Column(String(128), default="")                        # 生成所用模型
    prompt_tokens = Column(Integer, default=0)                     # 冗余记录；成本核算走 task_type=article 流水
    completion_tokens = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)
    error = Column(String(512), default="")                        # 失败原因（status=failed 时展示）
    gen_count = Column(Integer, nullable=False, default=1)         # 生成次数（重新生成 +1）
    revision = Column(Integer, nullable=False, default=1)           # 乐观锁版本，避免覆盖他人编辑
    created_by = Column(Integer)                                   # user.id，仅追溯
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
