"""事件聚类纯函数测试：三段式语义门槛（高位带直通 / 模糊带需关键词+跨来源 /
低位带拒绝）、时间窗、标题代表选举、事件级二阶段合并判定。

不依赖数据库；阈值显式传参，与 .env 解耦。
带级设计标定见 event_cluster.HIGH_BAND 注释（2026-09-09 bge-small-zh 真实向量）。
"""
from datetime import datetime, timedelta, timezone

from app.ai.event_cluster import (
    ClusterItem,
    cluster_articles,
    effective_thresholds,
    event_title_for,
    events_mergeable,
    keyword_jaccard_of,
    similarity_gate_pass,
)
from app.core.config import get_settings

SIM, KW, WIN = 0.85, 0.2, 48
BASE = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _item(id_: int, title: str, vec, hours_ago: float = 0, source: str = "IT之家") -> ClusterItem:
    return ClusterItem(
        id=id_,
        title=title,
        embedding=list(vec),
        publish_time=BASE - timedelta(hours=hours_ago),
        source=source,
    )


def _mid_vec() -> list[float]:
    """与 (1,0) 余弦 0.86 的单位向量：落在 [0.85, 0.89) 模糊带。"""
    return [0.86, 0.51]


# ———— cluster_articles：三段式门槛 ————

def test_high_band_merges_unconditionally():
    """sim=1.0 ≥ 高位带：关键词零重合、同来源也直通合并（语义几乎同一事件）。"""
    a = _item(1, "火星探测器发射成功", [1.0, 0.0])
    b = _item(2, "红烧牛肉面制作教程", [1.0, 0.0], hours_ago=2, source="快科技")
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 1


def test_mid_band_requires_keywords_and_cross_source():
    """模糊带：豆包场景（相似 0.86 + 跨来源 + 关键词达标）→ 合并。"""
    a = _item(1, "豆包输入法Windows版正式上线", [1.0, 0.0], source="快科技")
    b = _item(2, "豆包输入法推出 Windows 版覆盖五大平台", _mid_vec(), hours_ago=2, source="IT之家")
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=0.15, window_hours=WIN)
    assert len(clusters) == 1


def test_mid_band_same_source_blocked():
    """模糊带：Win11 学院场景（相似达标 + 同源同栏目）→ 不合并。"""
    a = _item(1, "Win11 学院：账户头像设置教程", _mid_vec(), source="IT之家")
    b = _item(2, "Win11 学院：任务栏手动启用教程", [1.0, 0.0], hours_ago=2, source="IT之家")
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=0.15, window_hours=WIN)
    assert len(clusters) == 2


def test_keyword_gate_blocks_merge_in_mid_band():
    """模糊带 + 跨来源，但标题关键词零重合：必须分开。"""
    a = _item(1, "火星探测器发射成功", [1.0, 0.0], source="快科技")
    b = _item(2, "红烧牛肉面制作教程", _mid_vec(), hours_ago=2, source="IT之家")
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 2


def test_sim_gate_blocks_merge():
    """向量正交（sim=0 < 阈值）：同标题也必须分开。"""
    a = _item(1, "火星探测器发射成功", [1.0, 0.0])
    b = _item(2, "火星探测器发射成功", [0.0, 1.0], hours_ago=2)
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 2


def test_time_window_blocks_merge():
    """同向量同标题但超出时间窗：必须分开；窗口内则合并。"""
    title, vec = "火星探测器发射成功", [1.0, 0.0]
    a = _item(1, title, vec, hours_ago=72)
    b = _item(2, title, vec, hours_ago=0)
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 2

    c = _item(3, title, vec, hours_ago=12)
    clusters = cluster_articles([b, c], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 1


def test_centroid_updates_with_members():
    """并簇后质心随成员更新：不再等于首个成员向量。"""
    items = [
        _item(1, "火星探测器发射成功", [1.0, 0.0], hours_ago=4),
        _item(2, "火星探测器成功入轨", [0.9, 0.1], hours_ago=3),
        _item(3, "火星探测器开始传回图像", [0.8, 0.2], hours_ago=2),
    ]
    clusters = cluster_articles(items, sim_threshold=0.5, keyword_threshold=0.05, window_hours=WIN)
    assert len(clusters) == 1
    c = clusters[0]
    assert len(c.members) == 3
    assert c.centroid != list(items[0].embedding)


# ———— similarity_gate_pass：带级判定 ————

def test_similarity_gate_pass_bands():
    kw_ok, srcs = 0.5, ({"快科技"}, {"IT之家"})
    assert not similarity_gate_pass(0.84, kw_ok, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    assert similarity_gate_pass(0.86, kw_ok, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    assert similarity_gate_pass(0.90, 0.0, *srcs, sim_threshold=0.85, keyword_threshold=0.2)  # 高位带
    assert not similarity_gate_pass(0.86, 0.1, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    assert not similarity_gate_pass(0.86, kw_ok, {"IT之家"}, {"IT之家"}, sim_threshold=0.85, keyword_threshold=0.2)


# ———— 关键词集合与阈值解析 ————

def test_keyword_jaccard_of_empty_sets():
    assert keyword_jaccard_of(set(), {"a"}) == 0.0
    assert keyword_jaccard_of(set(), set()) == 0.0


def test_cluster_item_kw_precomputed():
    """关键词集合构造时一次算好（O(n) 次分词而非逐对 O(n²) 次）。"""
    item = _item(1, "火星探测器发射成功", [1.0, 0.0])
    assert item.kw_set
    assert item.kw_set is item.kw_set  # 同一对象，未重复计算


def test_effective_thresholds_fallback_and_override():
    s = get_settings()
    assert effective_thresholds(None, None, None) == (
        s.sim_threshold,
        s.keyword_jaccard_threshold,
        s.cluster_window_hours,
    )
    assert effective_thresholds(0.5, 0.1, 7) == (0.5, 0.1, 7)


# ———— event_title_for：代表选举 ————

def test_event_title_for_single_member():
    assert event_title_for([_item(1, "唯一报道标题", [1.0])]) == "唯一报道标题"


def test_event_title_for_picks_most_representative():
    members = [
        _item(1, "苹果发布iPhone 17手机", [1.0, 0.0, 0.0]),
        _item(2, "iPhone 17手机价格公布", [0.0, 1.0, 0.0]),
        _item(3, "华为Mate 70手机开售", [0.0, 0.0, 1.0]),
    ]
    title = event_title_for(members)
    assert ("苹果" in title) or ("iPhone" in title or "iphone" in title.lower())
    assert "华为" not in title


# ———— events_mergeable：事件级二阶段合并判定 ————

def _event_params(hours_gap_between_events: float):
    kw = {"豆包", "输入法", "windows"}
    first_a, last_a = BASE - timedelta(hours=10), BASE - timedelta(hours=8)
    first_b = last_a + timedelta(hours=hours_gap_between_events)
    last_b = first_b + timedelta(hours=2)
    return kw, first_a, last_a, first_b, last_b


def test_events_mergeable_true_cross_source_mid_band():
    kw, fa, la, fb, lb = _event_params(hours_gap_between_events=1)
    assert events_mergeable(
        [1.0, 0.0], [0.86, 0.51], kw, kw, {"快科技"}, {"IT之家"}, fa, la, fb, lb,
        sim_threshold=0.85, keyword_threshold=0.15, window_hours=WIN,
    )


def test_events_mergeable_false_same_source_mid_band():
    """模糊带 + 同源（Win11 学院陷阱）：即使 sim 超阈值也不合并。"""
    kw, fa, la, fb, lb = _event_params(hours_gap_between_events=1)
    assert not events_mergeable(
        [1.0, 0.0], [0.86, 0.51], kw, kw, {"IT之家"}, {"IT之家"}, fa, la, fb, lb,
        sim_threshold=0.85, keyword_threshold=0.15, window_hours=WIN,
    )


def test_events_mergeable_false_when_gap_exceeds_window():
    kw, fa, la, fb, lb = _event_params(hours_gap_between_events=60)
    assert not events_mergeable(
        [1.0, 0.0], [0.99, 0.1], kw, kw, {"快科技"}, {"IT之家"}, fa, la, fb, lb,
        sim_threshold=0.85, keyword_threshold=0.15, window_hours=WIN,
    )


def test_events_mergeable_false_when_keywords_dissimilar():
    kw, fa, la, fb, lb = _event_params(hours_gap_between_events=1)
    assert not events_mergeable(
        [1.0, 0.0], [0.86, 0.51], kw, {"完全", "无关"}, {"快科技"}, {"IT之家"}, fa, la, fb, lb,
        sim_threshold=0.85, keyword_threshold=0.15, window_hours=WIN,
    )


def test_events_mergeable_false_when_centroids_dissimilar():
    kw, fa, la, fb, lb = _event_params(hours_gap_between_events=1)
    assert not events_mergeable(
        [1.0, 0.0], [0.0, 1.0], kw, kw, {"快科技"}, {"IT之家"}, fa, la, fb, lb,
        sim_threshold=0.85, keyword_threshold=0.15, window_hours=WIN,
    )
