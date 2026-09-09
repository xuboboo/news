"""只读审计：校验库内文章是否都来自频道页面且频道归属正确。

以 PAGE_SOURCES 页面实时链接为基准，逐篇核对：
- 频道不符（URL 出现在其他频道的页面上）→ 硬违规；
- 不在任何页面：若文章比该频道在库、仍在页面上的最旧一篇更旧，
  属于页面自然轮换（正常），否则为可疑违规（页面覆盖该时段却没有它）。

存在硬违规时退出码 1。只读，不修改任何数据。

用法（容器内）：python scripts/audit_sources.py
"""
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.core.db import SessionLocal
from app.crawler.ithome import PAGE_SOURCES, UA, extract_article_links
from app.models.article import Article


def build_page_channel_map() -> dict[str, str]:
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


def main() -> int:
    db = SessionLocal()
    try:
        print("[1/2] 抓取频道页面基准链接 ...")
        url_channel = build_page_channel_map()
        if not url_channel:
            print("所有页面均不可达，无法审计。")
            return 2

        rows = (
            db.query(Article)
            # 仅审计 IT之家：快科技等来源以文章页面包屑为真相，不经 ithome 页面语义
            .filter(Article.source == "IT之家")
            .all()
        )
        print(f"[2/2] 库内共 {len(rows)} 篇，逐篇核对 ...\n")

        # 每个频道的「页面覆盖下界」= 该频道在库且仍在页面上的最旧一篇发布时间；
        # 比它还旧却不在页面上 → 页面已滚出（正常轮换），比它新却不在页面上 → 可疑
        boundary: dict[str, datetime | None] = {}
        for a in rows:
            ch = url_channel.get(a.url)
            if ch is not None:
                cur = boundary.get(ch)
                if cur is None or a.publish_time < cur:
                    boundary[ch] = a.publish_time

        ok = mismatch = suspicious = rotated = 0
        channel_dist: Counter = Counter()
        issues: list[str] = []
        rotated_notes: list[str] = []
        for a in rows:
            expected = url_channel.get(a.url)
            channel_dist[a.channel] += 1
            if expected is not None and expected != a.channel:
                mismatch += 1
                issues.append(f"  频道不符: #{a.id} 库内[{a.channel}] 页面[{expected}] {a.title[:44]}")
            elif expected is None:
                b = boundary.get(a.channel)
                if b is not None and a.publish_time >= b:
                    suspicious += 1
                    issues.append(f"  可疑(比页面下界新却不在页面): #{a.id} [{a.channel}] {a.title[:40]} | {a.url}")
                else:
                    rotated += 1
                    rotated_notes.append(f"  已滚出页面窗口: #{a.id} [{a.channel}] {a.title[:44]}")
            else:
                ok += 1

        print("各频道在库数量：")
        for ch, n in channel_dist.most_common():
            print(f"  {ch}: {n}")
        print(f"\n结果：匹配 {ok} · 频道不符 {mismatch} · 可疑 {suspicious} · 已滚出页面 {rotated}")
        if issues:
            print("违规清单：")
            for line in issues:
                print(line)
        if rotated_notes:
            print(f"轮换说明（非违规，前 10 / 共 {rotated}）：")
            for line in rotated_notes[:10]:
                print(line)
        verdict = "通过" if (mismatch == 0 and suspicious == 0) else "存在违规"
        print(f"\n审计结论：{verdict}")
        return 0 if verdict == "通过" else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
