"""事件聚类纯函数测试：三段式语义门槛（高位带需跨来源+关键词地面值 / 模糊带
需关键词+跨来源 / 低位带拒绝）、时间窗、标题代表选举、事件级二阶段合并判定。

不依赖数据库；阈值显式传参，与 .env 解耦。
带级设计标定见 event_cluster.HIGH_BAND 注释（2026-09-09 bge-small-zh 真实向量）；
2026-09-09 审计修正后高位带不再零条件直通，含真实误聚类样本回归用例。
"""
from datetime import datetime, timedelta, timezone

from app.ai.event_cluster import (
    ClusterItem,
    cluster_articles,
    effective_thresholds,
    elect_title,
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

def test_high_band_zero_keyword_blocked():
    """审计修正：sim=1.0 高位带 + 关键词零重合（跨来源）→ 不再直通，必须分开。

    对应审计混题形态：向量偶然高位但主题完全无关（刑事案件 vs 无关科技文）。
    """
    a = _item(1, "火星探测器发射成功", [1.0, 0.0])
    b = _item(2, "红烧牛肉面制作教程", [1.0, 0.0], hours_ago=2, source="快科技")
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 2


def test_high_band_same_source_blocked():
    """审计修正：同来源高位相似一律不并（Win11 学院同栏目模板文形态，
    基线 0.90-0.96 恰落高位带，旧行为零条件直通正是污染入口）。"""
    a = _item(1, "Win11 学院：账户头像设置教程", [1.0, 0.0], source="IT之家")
    b = _item(2, "Win11 学院：任务栏手动启用教程", [1.0, 0.0], hours_ago=2, source="IT之家")
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 2


def test_high_band_cross_source_with_keyword_evidence_merges():
    """高位带 + 跨来源 + 关键词重合达地面值（≥0.1）：正常合并。"""
    a = _item(1, "豆包输入法Windows版正式上线", [1.0, 0.0], source="快科技")
    b = _item(2, "豆包输入法推出Windows版覆盖全平台", [1.0, 0.0], hours_ago=2, source="IT之家")
    clusters = cluster_articles(
        [a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN,
        high_band_kw_floor=0.05,
    )
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
    """同向量同标题但超出时间窗：必须分开；窗口内（跨来源佐证）则合并。"""
    title, vec = "火星探测器发射成功", [1.0, 0.0]
    a = _item(1, title, vec, hours_ago=72)
    b = _item(2, title, vec, hours_ago=0)
    clusters = cluster_articles([a, b], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 2

    c = _item(3, title, vec, hours_ago=12, source="快科技")
    clusters = cluster_articles([b, c], sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN)
    assert len(clusters) == 1


def test_centroid_updates_with_members():
    """并簇后质心随成员更新：不再等于首个成员向量（事件成员跨来源，同真实形态）。"""
    items = [
        _item(1, "火星探测器发射成功", [1.0, 0.0], hours_ago=4),
        _item(2, "火星探测器成功入轨", [0.9, 0.1], hours_ago=3, source="快科技"),
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
    # 高位带：零关键词重合不再直通（审计修正）
    assert not similarity_gate_pass(0.90, 0.0, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    # 高位带：跨来源 + 重合达地面值 → 合并；重合低于地面值（泛词单词重合形态）→ 拒绝
    assert similarity_gate_pass(0.90, 0.1, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    assert not similarity_gate_pass(0.90, 0.06, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    # 高位带：同来源一律不并——即使关键词过正式阈值（同源高位相似不可信）
    assert not similarity_gate_pass(0.90, 0.5, {"IT之家"}, {"IT之家"}, sim_threshold=0.85, keyword_threshold=0.2)
    assert not similarity_gate_pass(0.86, 0.1, *srcs, sim_threshold=0.85, keyword_threshold=0.2)
    assert not similarity_gate_pass(0.86, kw_ok, {"IT之家"}, {"IT之家"}, sim_threshold=0.85, keyword_threshold=0.2)


def test_audit_negative_samples_never_merge():
    """审计回归（2026-09-09）：真实混题样本——刑事案件标题曾吸附以下无关科技文。

    极端向量（sim=1.0，随机负样本亦可达成）+ 跨来源组合下，全部保持独立簇。"""
    crime = "六人利用AI越狱生成淫秽物品最终获刑 法官提醒技术中立不等于技术无罪"
    unrelated = [
        "A16z合伙人：AI将重塑风险投资行业格局",
        "NOVA2.0具身智能平台发布 机器人进工厂",
        "美国国防部与OpenAI达成合作",
        "京东物流机器人密集上岗应对年末快递",
        "Arm CEO：芯片架构演进应对AI电力需求",
        "Meta眼镜新专利曝光",
    ]
    items = [_item(1, crime, [1.0, 0.0])]
    for i, title in enumerate(unrelated):
        items.append(
            _item(i + 2, title, [1.0, 0.0], hours_ago=i + 1, source="快科技" if i % 2 else "IT之家")
        )
    clusters = cluster_articles(
        items, sim_threshold=SIM, keyword_threshold=KW, window_hours=WIN,
        high_band_kw_floor=0.1,
    )
    assert len(clusters) == len(items)


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


# ———— best-fit 归属（2026-09-10 审计）与标题选举核心 ————

def test_cluster_assignment_picks_best_fit_not_first_hit():
    """best-fit 回归：文章同时过多个簇的门时，并入相似度最高者（与事件并入
    「取最高者」同口径），而非旧实现首中即停吸给先建簇。

    构造：同源双种子（IT之家，同源规则保证互不合并 → 必成两簇）先后建簇，
    第三方来源文章与先建簇 0.87（模糊带）、与后建簇 ≈1.0（高位带）双过门，
    必须归入后者。
    """
    a = _item(1, "豆包输入法Windows版正式上线", [1.0, 0.0], hours_ago=1, source="IT之家")
    b = _item(2, "豆包输入法推出 Windows 版覆盖五大平台", [0.86, 0.51], hours_ago=2, source="IT之家")
    x = _item(3, "豆包输入法推出Windows版覆盖全平台", [0.87, 0.49], hours_ago=3, source="快科技")
    clusters = cluster_articles(
        [a, b, x], sim_threshold=SIM, keyword_threshold=0.05, window_hours=WIN
    )
    assert len(clusters) == 2
    by_seed_a = next(c for c in clusters if any(m.id == 1 for m in c.members))
    by_seed_b = next(c for c in clusters if any(m.id == 2 for m in c.members))
    assert {m.id for m in by_seed_a.members} == {1}
    assert {m.id for m in by_seed_b.members} == {2, 3}


def test_elect_title_matches_cluster_election_and_single_member():
    """elect_title（事件成长重选举共用核心）与建簇选举口径一致；单成员取原标题。"""
    members = [
        _item(1, "苹果发布iPhone 17手机", [1.0, 0.0, 0.0]),
        _item(2, "iPhone 17手机价格公布", [0.0, 1.0, 0.0]),
        _item(3, "华为Mate 70手机开售", [0.0, 0.0, 1.0]),
    ]
    titles = [m.title for m in members]
    kws = [m.kw_set for m in members]
    assert elect_title(titles, kws) == event_title_for(members)
    assert elect_title(["唯一标题"], [{"a"}]) == "唯一标题"
