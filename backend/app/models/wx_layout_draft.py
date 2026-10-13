"""公众号排版草稿模型：每日早报 → 公众号后台可直接粘贴的内联样式 HTML。

与 article_draft（选题生文）独立的第三条草稿链路：源是 daily_report 的内容
快照而非事件/资讯，产物是排版完成的整篇 HTML 而非 Markdown。源早报可能被
重建/清理，草稿独立存活，report_date 冗余快照保证列表展示不依赖源行。

一期为确定性排版（不经 LLM），同一天早报只有一份排版草稿
（uq_wx_layout_report），重复生成走覆盖更新（gen_count +1）。
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Date, DateTime, Integer, String, Text, UniqueConstraint

from app.core.db import Base


def _utcnow():
    return datetime.now(timezone.utc)


class WxLayoutDraft(Base):
    __tablename__ = "wx_layout_draft"
    __table_args__ = (UniqueConstraint("report_id", name="uq_wx_layout_report"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, nullable=False, index=True)  # daily_report.id，不设 FK（同 article_draft 先例）
    report_date = Column(Date, nullable=False, index=True)   # 早报发布日（北京时间口径），列表展示/排序用
    issue_no = Column(Integer, nullable=False, default=1)
    title = Column(String(128), nullable=False, default="")  # 拟发布的公众号标题（后台单独填写）
    digest = Column(String(200), nullable=False, default="")  # 公众号摘要，取早报综述
    content_html = Column(Text, nullable=False, default="")  # 整篇内联样式 HTML（含固定开头/结尾）
    gen_count = Column(Integer, nullable=False, default=1)   # 生成次数（重新生成 +1）
    revision = Column(Integer, nullable=False, default=1)    # 乐观锁版本，与删除接口配套
    created_by = Column(Integer)                             # user.id，仅追溯
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
