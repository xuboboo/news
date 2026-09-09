"""热点事件聚合算法（PRD 第 10 节）

判定条件（同时满足才合并为同一事件）：
1. Embedding 余弦相似度 > 生效阈值（默认 0.85，管理面板可热调）
2. 标题关键词 Jaccard 相似度 ≥ 阈值（关键词层面，管理面板可热调）
3. 发布时间在聚类时间窗内（默认 48 小时，管理面板可热调）

聚类采用贪心最近质心法：按时间倒序逐篇处理，命中已有事件则并入并更新质心，
否则开新事件。事件级二阶段合并（events_mergeable）与建簇同门槛，用于修复
"同一事件的文章跨轮次先后到达、各自成事件后不再合并"的拆分问题。

性能：标题关键词集合在 ClusterItem 构造时一次性预计算（jieba 分词是全流程
最贵的一步，逐对重复计算会使单轮聚类开销从 O(n) 次分词退化到 O(n²) 次）。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

from app.ai.embedding import centroid, cosine
from app.core.config import get_settings
from app.utils.text import extract_keywords

# 模糊带宽度：sim_threshold ~ sim_threshold+HIGH_BAND 之间为「语义相近但不确定」，
# 需关键词 + 跨来源佐证；高位带视为几乎同一事件。
# 2026-09-09 在 bge-small-zh-v1.5 真实向量上标定：同事件对 ≥0.94，Win11 单一
# 语料的同域基线高达 0.90+（同栏目不同教程 0.96），单一 (sim∧kw) 门槛无法分离，
# 须按带分级 + 来源交叉判断（同事件跨来源正是 PRD 的多来源聚合语义）。
# 2026-09-09 审计修正：高位带不再零条件直通——随机无关对也能摸到高位
# （审计实测「六人利用 AI 获刑」事件混入 ARM 研究/豆包博物馆等完全无关文章），
# 且 Win11 同栏目基线 0.90-0.96 恰好落在高位带内。现高位带同样要求跨来源，
# 关键词重合只放宽到地面值 HIGH_BAND_KW_FLOOR（泛词如"AI"单词重合 ≈0.06
# 挡在门外，真实同事件标题通常共享专名、重合 ≥0.2）；同来源一律不走高位带。
HIGH_BAND = 0.04
HIGH_BAND_KW_FLOOR = 0.1


@dataclass
class ClusterItem:
    id: int
    title: str
    embedding: List[float]
    publish_time: datetime
    channel: str = ""
    source: str = ""
    kw: Set[str] = field(default_factory=set)

    def __post_init__(self):
        if not self.kw:
            self.kw = set(extract_keywords(self.title, 10))

    @property
    def kw_set(self) -> Set[str]:
        return self.kw


@dataclass
class Cluster:
    members: List[ClusterItem] = field(default_factory=list)
    centroid: List[float] = field(default_factory=list)
    sources: Set[str] = field(default_factory=set)

    @property
    def newest_time(self) -> Optional[datetime]:
        return max((m.publish_time for m in self.members), default=None)


def similarity_gate_pass(
    sim: float,
    kw: float,
    sources_a: Set[str],
    sources_b: Set[str],
    sim_threshold: float,
    keyword_threshold: float,
    high_band_kw_floor: float = HIGH_BAND_KW_FLOOR,
) -> bool:
    """三段式语义门槛（真实向量标定，见 HIGH_BAND 注释）：

    - 低位带 sim < sim_threshold：不合并；
    - 高位带 sim ≥ sim_threshold + HIGH_BAND：跨来源佐证 + 关键词重合不低于
      HIGH_BAND_KW_FLOOR（审计修正：不再零条件直通，同来源高位相似是
      Win11 学院类同栏目模板文的形态，一律不并）；
    - 模糊带 sim_threshold ≤ sim < 高位带：需标题关键词 ≥ 阈值，且双方来源
      合并后 ≥2 个不同来源（跨来源佐证正是 PRD「多篇不同报道」的聚合语义）。
    """
    if sim < sim_threshold:
        return False
    cross_source = len(set(sources_a) | set(sources_b)) >= 2
    if sim >= sim_threshold + HIGH_BAND:
        return cross_source and kw >= high_band_kw_floor
    if kw < keyword_threshold:
        return False
    return cross_source


def _tz(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def keyword_jaccard_of(ka: Set[str], kb: Set[str]) -> float:
    """预计算关键词集合的 Jaccard 相似度（keyword_jaccard 的免分词版本）。"""
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def effective_thresholds(
    sim_threshold: Optional[float] = None,
    keyword_threshold: Optional[float] = None,
    window_hours: Optional[int] = None,
) -> tuple[float, float, int]:
    """运行时生效阈值解析：传入 None 的项回落 .env 静态配置。"""
    s = get_settings()
    sim_th = s.sim_threshold if sim_threshold is None else sim_threshold
    kw_th = s.keyword_jaccard_threshold if keyword_threshold is None else keyword_threshold
    win = s.cluster_window_hours if window_hours is None else window_hours
    return sim_th, kw_th, win


def cluster_articles(
    items: List[ClusterItem],
    sim_threshold: Optional[float] = None,
    keyword_threshold: Optional[float] = None,
    window_hours: Optional[int] = None,
    high_band_kw_floor: float = HIGH_BAND_KW_FLOOR,
) -> List[Cluster]:
    """贪心聚类：时间倒序处理，满足三条件（相似度/关键词/时间窗）才并入。

    阈值未传时取 .env 静态配置；生产链路由 run_event_clustering 传入
    运行时生效值（DB 覆盖 .env，管理面板热调）。

    时间窗以簇内最新成员（members[0]，时间倒序处理的首个成员）为基准，
    即簇内所有成员与最新成员的发布时间差不超过窗口。
    """
    sim_th, kw_th, win_h = effective_thresholds(sim_threshold, keyword_threshold, window_hours)
    window = win_h * 3600
    ordered = sorted(items, key=lambda m: _tz(m.publish_time), reverse=True)

    clusters: List[Cluster] = []
    for item in ordered:
        placed = False
        for c in clusters:
            rep = c.members[0]
            # 条件 3：发布时间窗（对簇内最新成员）
            if abs((_tz(item.publish_time) - _tz(rep.publish_time)).total_seconds()) > window:
                continue
            # 条件 1+2：三段式语义门槛（相似度带级 + 关键词 + 来源交叉）
            if not similarity_gate_pass(
                cosine(item.embedding, c.centroid),
                keyword_jaccard_of(item.kw_set, rep.kw_set),
                c.sources,
                {item.source},
                sim_th,
                kw_th,
                high_band_kw_floor,
            ):
                continue
            c.members.append(item)
            c.sources.add(item.source)
            c.centroid = centroid([m.embedding for m in c.members])
            placed = True
            break
        if not placed:
            clusters.append(
                Cluster(members=[item], centroid=list(item.embedding), sources={item.source})
            )
    return clusters


def event_title_for(members: List[ClusterItem]) -> str:
    """事件标题：选与其余成员关键词重合度总和最高（最核心）的成员标题，截短展示。

    平票时取更长标题（信息量更大）；单成员直接用原标题。
    """
    if len(members) == 1:
        return members[0].title
    best = max(
        range(len(members)),
        key=lambda i: (
            sum(keyword_jaccard_of(members[i].kw_set, other.kw_set) for j, other in enumerate(members) if j != i),
            len(members[i].title),
        ),
    )
    return members[best].title.strip()[:120]


def event_time_range(members: List[ClusterItem]):
    times = [_tz(m.publish_time) for m in members]
    return min(times), max(times)


def events_mergeable(
    centroid_a: Optional[List[float]],
    centroid_b: Optional[List[float]],
    kw_a: Set[str],
    kw_b: Set[str],
    sources_a: Set[str],
    sources_b: Set[str],
    first_a: datetime,
    last_a: datetime,
    first_b: datetime,
    last_b: datetime,
    sim_threshold: Optional[float] = None,
    keyword_threshold: Optional[float] = None,
    window_hours: Optional[int] = None,
    high_band_kw_floor: float = HIGH_BAND_KW_FLOOR,
) -> bool:
    """事件级二阶段合并判定（纯函数）：时间窗相邻 + 三段式语义门槛（同建簇口径）。

    时间窗按事件跨度区间 [first, last] 的间隔计——两事件时间区间有重叠或
    间隔不超过窗口才算时间上相邻。
    """
    sim_th, kw_th, _win_h = effective_thresholds(sim_threshold, keyword_threshold, window_hours)
    window = timedelta(hours=_win_h)

    gap = max(_tz(first_a), _tz(first_b)) - min(_tz(last_a), _tz(last_b))
    if gap > window:
        return False
    sim = cosine(centroid_a, centroid_b)
    kw = keyword_jaccard_of(kw_a, kw_b)
    return similarity_gate_pass(sim, kw, sources_a, sources_b, sim_th, kw_th, high_band_kw_floor)
