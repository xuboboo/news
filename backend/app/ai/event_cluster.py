"""热点事件聚合算法（PRD 第 10 节）

判定条件（同时满足才合并为同一事件）：
1. Embedding 余弦相似度 > 0.85（与事件质心比较）
2. 标题关键词 Jaccard 相似度 ≥ 阈值（关键词层面）
3. 发布时间在聚类时间窗内（默认 48 小时）

聚类采用贪心最近质心法：按时间倒序逐篇处理，命中已有事件则并入并更新质心，
否则开新事件。实体名称包含在分词关键词中自然参与比较。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.ai.embedding import centroid, cosine
from app.core.config import get_settings
from app.utils.text import keyword_jaccard


@dataclass
class ClusterItem:
    id: int
    title: str
    embedding: List[float]
    publish_time: datetime
    channel: str = ""
    source: str = ""


@dataclass
class Cluster:
    members: List[ClusterItem] = field(default_factory=list)
    centroid: List[float] = field(default_factory=list)

    @property
    def newest_time(self) -> Optional[datetime]:
        return max((m.publish_time for m in self.members), default=None)


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def cluster_articles(items: List[ClusterItem]) -> List[Cluster]:
    """贪心聚类：时间倒序处理，满足三条件（相似度/关键词/时间窗）才并入。"""
    s = get_settings()
    window = s.cluster_window_hours * 3600
    ordered = sorted(items, key=lambda m: _tz(m.publish_time), reverse=True)

    clusters: List[Cluster] = []
    for item in ordered:
        placed = False
        for c in clusters:
            rep = c.members[0]
            # 条件 3：发布时间窗
            if abs((_tz(item.publish_time) - _tz(rep.publish_time)).total_seconds()) > window:
                continue
            # 条件 1：Embedding 相似度（对质心）
            sim = cosine(item.embedding, c.centroid)
            # 条件 2：标题关键词重合
            kw = keyword_jaccard(item.title, rep.title)
            if sim > s.sim_threshold and kw >= s.keyword_jaccard_threshold:
                c.members.append(item)
                c.centroid = centroid([m.embedding for m in c.members])
                placed = True
                break
        if not placed:
            clusters.append(Cluster(members=[item], centroid=list(item.embedding)))
    return clusters


def event_title_for(members: List[ClusterItem]) -> str:
    """事件标题：选关键词覆盖度最高（最核心）的成员标题，截短展示。"""
    if len(members) == 1:
        return members[0].title
    from app.utils.text import extract_keywords

    all_kws = [set(extract_keywords(m.title, 10)) for m in members]
    union = set().union(*all_kws)
    best = max(zip(members, all_kws), key=lambda pair: len(pair[1] & union))
    title = best[0].title.strip()
    return title[:120]


def event_time_range(members: List[ClusterItem]):
    times = [_tz(m.publish_time) for m in members]
    return min(times), max(times)
