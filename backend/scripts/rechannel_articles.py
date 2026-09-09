"""存量文章频道修复：以频道页面的实时链接为准（页面即真相）。

对库内 IT之家文章（仅 source="IT之家"，其他来源如快科技不在此语义内）：
- URL 出现在某频道页面 → 频道改为该页面频道（优先级与爬虫 PAGE_SOURCES 一致，先到先得）
- URL 未出现在任何频道页面 → 删除（摘要随 ORM 级联删除，事件关联由 FK CASCADE 清理，
  收尾时再删除成员为 0 的热点事件）；
  但比该频道「页面覆盖下界」（在库且仍在页面上的最旧一篇）更旧的属页面自然轮换，保留

用法（容器内）：python scripts/rechannel_articles.py
"""
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import func

from app.core.db import SessionLocal
from app.crawler.ithome import PAGE_SOURCES, UA, extract_article_links
from app.models.article import Article
from app.models.event import EventArticle, HotEvent


def build_page_channel_map() -> dict[str, str]:
    """抓取全部频道页面，返回 文章URL → 频道（setdefault 先到先得，优先级同爬虫）。"""
    url_channel: dict[str, str] = {}
    for channel, pages in PAGE_SOURCES.items():
        for page in pages:
            try:
                resp = httpx.get(page, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
            except Exception as exc:
                print(f"  ! 页面抓取失败 {page}: {exc}")
                continue
            if resp.status_code != 200:
                print(f"  ! 页面不可达 HTTP {resp.status_code} {page}")
                continue
            for link in extract_article_links(resp.text):
                url_channel.setdefault(link, channel)
            print(f"  √ {channel} ← {page}（映射累计 {len(url_channel)} 篇）")
    return url_channel


def main() -> None:
    db = SessionLocal()
    try:
        print("[1/4] 抓取频道页面，建立 页面URL→频道 映射 ...")
        url_channel = build_page_channel_map()
        if not url_channel:
            print("所有页面均不可达，中止（不做任何修改）。")
            return
        # 熔断护栏：任一频道一个链接都没抓到（该频道页面全部不可达 / 改版致解析
        # 失效）时，该频道在库文章会因 boundary 缺失被整体判为可删——这正是本脚本
        # 唯一可能真实丢数据的路径。此时立即中止，不做任何修改。
        mapped_channels = set(url_channel.values())
        dead_channels = [ch for ch in PAGE_SOURCES if ch not in mapped_channels]
        if dead_channels:
            print(f"频道 {dead_channels} 的页面全部不可达或解析为空，中止（不做任何修改）。")
            return

        rows = (
            db.query(Article)
            # 仅限 IT之家：本脚本按 ithome 页面判定，库外来源（快科技以面包屑为真相，
            # 不在 ithome 页面上）不参与，否则会被整体误删
            .filter(Article.source == "IT之家")
            .all()
        )
        before = Counter(a.channel for a in rows)

        # 每个频道的「页面覆盖下界」= 该频道在库且仍在页面上的最旧一篇发布时间；
        # 比它旧却不在页面上属于页面自然轮换（保留），比它新却不在页面上才删除
        boundary: dict[str, datetime | None] = {}
        for a in rows:
            ch = url_channel.get(a.url)
            if ch is not None:
                cur = boundary.get(ch)
                if cur is None or a.publish_time < cur:
                    boundary[ch] = a.publish_time

        print(f"[2/4] 库内共 {len(rows)} 篇，开始比对 ...")

        rechanneled = deleted = rotated = 0
        deleted_titles: list[str] = []
        for a in rows:
            channel = url_channel.get(a.url)
            if channel is None:
                b = boundary.get(a.channel)
                if b is not None and a.publish_time < b:
                    rotated += 1  # 已滚出页面窗口的正常旧文，保留
                    continue
                deleted_titles.append(f"[{a.channel}] {a.title[:46]}")
                db.delete(a)
                deleted += 1
                continue
            if a.channel != channel:
                print(f"  纠偏 #{a.id} {a.channel} → {channel} | {a.title[:40]}")
                a.channel = channel
                rechanneled += 1
        db.commit()

        print("[3/4] 清理无成员的热点事件 ...")
        counts = dict(
            db.query(EventArticle.event_id, func.count(EventArticle.id))
            .group_by(EventArticle.event_id)
            .all()
        )
        empty_ids = [ev.id for ev in db.query(HotEvent).all() if counts.get(ev.id, 0) == 0]
        if empty_ids:
            db.query(HotEvent).filter(HotEvent.id.in_(empty_ids)).delete(synchronize_session=False)
            db.commit()
        print(f"  删除空事件 {len(empty_ids)} 个")

        after_rows = db.query(Article.channel, func.count(Article.id)).group_by(Article.channel).all()
        after = Counter(dict(after_rows))
        print(f"[4/4] 完成：纠偏 {rechanneled} 篇 · 删除 {deleted} 篇 · 保留轮换旧文 {rotated} 篇 · 空事件 {len(empty_ids)} 个")
        print("各频道数量（修复前 → 修复后）：")
        for ch in sorted(set(before) | set(after)):
            print(f"  {ch}: {before.get(ch, 0)} → {after.get(ch, 0)}")
        if deleted_titles:
            print(f"删除清单（前 20 / 共 {len(deleted_titles)}）：")
            for t in deleted_titles[:20]:
                print(f"  - {t}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
