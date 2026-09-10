"""热点事件聚合算法（PRD 第 10 节）

判定条件（同时满足才合并为同一事件）：
1. Embedding 余弦相似度 > 生效阈值（默认 0.85，管理面板可热调）
2. 标题关键词 Jaccard 相似度 ≥ 阈值（关键词层面，管理面板可热调）
3. 发布时间在聚类时间窗内（默认 48 小时，管理面板可热调）

聚类采用贪心质心法：按时间倒序逐篇处理，在全部门槛通过的簇中取相似度
最高者并入（best-fit，平票取成员多者/先建簇）并更新质心，否则开新簇。
事件级二阶段合并（events_mergeable）与建簇同门槛，用于修复
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

    归属为 best-fit：与全部过门簇中相似度最高者合并（与事件并入「取最高者」
    同口径），平票取成员多者、再取先建簇（更新）。旧实现首中即停（first-fit），
    文章可能被吸给成立更早/更晚但匹配略差的簇，与事件级口径分裂。
    代价是过门簇全量比较余弦——时间窗与关键词两条廉价检查前置，只有双过的
    簇才算余弦（两带关键词门槛的较小值以下必不过门，直接跳过最贵一步）。

    阈值未传时取 .env 静态配置；生产链路由 run_event_clustering 传入
    运行时生效值（DB 覆盖 .env，管理面板热调）。

    时间窗以簇内最新成员（members[0]，时间倒序处理的首个成员）为基准，
    即簇内所有成员与最新成员的发布时间差不超过窗口。
    """
    sim_th, kw_th, win_h = effective_thresholds(sim_threshold, keyword_threshold, window_hours)
    window = win_h * 3600
    kw_floor = min(kw_th, high_band_kw_floor)  # 两带关键词门槛的较小值
    ordered = sorted(items, key=lambda m: _tz(m.publish_time), reverse=True)

    clusters: List[Cluster] = []
    for item in ordered:
        best_score: Optional[tuple[float, int, int]] = None
        best_cluster: Optional[Cluster] = None
        for idx, c in enumerate(clusters):
            rep = c.members[0]
            # 条件 3：发布时间窗（对簇内最新成员）——最廉价的检查放最前
            if abs((_tz(item.publish_time) - _tz(rep.publish_time)).total_seconds()) > window:
                continue
            kw = keyword_jaccard_of(item.kw_set, rep.kw_set)
            if kw < kw_floor:
                continue
            # 条件 1+2：三段式语义门槛（相似度带级 + 关键词 + 来源交叉）
            sim = cosine(item.embedding, c.centroid)
            if not similarity_gate_pass(
                sim, kw, c.sources, {item.source}, sim_th, kw_th, high_band_kw_floor
            ):
                continue
            key = (sim, len(c.members), -idx)
            if best_score is None or key > best_score:
                best_score, best_cluster = key, c
        if best_cluster is not None:
            best_cluster.members.append(item)
            best_cluster.sources.add(item.source)
            best_cluster.centroid = centroid([m.embedding for m in best_cluster.members])
        else:
            clusters.append(
                Cluster(members=[item], centroid=list(item.embedding), sources={item.source})
            )
    return clusters


def elect_title(titles: List[str], kw_sets: List[Set[str]]) -> str:
    """标题选举核心：选与其余成员关键词重合度总和最高（最核心）的标题。

    平票时取更长标题（信息量更大）；单成员直接用原标题。
    titles 与 kw_sets 按下标一一对应；建簇（ClusterItem）与事件成长
    （Article 成员，event_service）共用同一口径。
    """
    if len(titles) == 1:
        return titles[0]
    best = max(
        range(len(titles)),
        key=lambda i: (
            sum(keyword_jaccard_of(kw_sets[i], kw) for j, kw in enumerate(kw_sets) if j != i),
            len(titles[i]),
        ),
    )
    return titles[best].strip()[:120]


def event_title_for(members: List[ClusterItem]) -> str:
    """事件标题选举（建簇时）：elect_title 的 ClusterItem 入口。"""
    return elect_title([m.title for m in members], [m.kw_set for m in members])


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
