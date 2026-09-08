"""抽取式摘要 v2 单测：真 TextRank + MMR 去冗余 + 结构约束（纯函数，无 DB）。"""
from app.ai.summary import _mmr_select, _summarize_extractive
from app.utils.text import extract_keywords

TITLE = "苹果发布搭载M5芯片的MacBook Pro 续航提升至24小时"
CONTENT = (
    "苹果公司今日凌晨召开新品发布会，正式推出搭载M5芯片的新款MacBook Pro。"
    "新款MacBook Pro性能较上一代提升约40%，图形处理能力提升近一倍。"
    "续航方面得益于M5芯片能效优化，新机最长续航达到24小时，创Mac历史新高。"
    "感谢关注本站报道，我们将持续跟进后续消息。"
    "价格方面14英寸起售价12999元，16英寸起售价15999元，本周五开启预购。"
    "分析师认为M5芯片的AI算力翻倍，将强化苹果在端侧大模型领域的竞争力。"
    "发布会结束后苹果公司股价盘前上涨2.3%，市值重回三万五千亿美元。"
)


def test_model_name_and_structure():
    r = _summarize_extractive(TITLE, CONTENT)
    assert r.model == "extractive-textrank-v2"
    assert 1 <= len(r.key_points) <= 5
    assert len(r.summary_short) <= 60
    assert len(r.summary_long) <= 400
    assert len(r.tags) <= 4
    assert all(len(t) > 1 for t in r.tags)


def test_key_points_in_original_order():
    r = _summarize_extractive(TITLE, CONTENT)
    # 样例句子均短于 70 字，要点可在原文中精确定位，验证按原文顺序输出
    idx = [CONTENT.index(p) for p in r.key_points]
    assert idx == sorted(idx)


def test_boilerplate_excluded():
    r = _summarize_extractive(TITLE, CONTENT)
    assert all("感谢" not in p for p in r.key_points)


def test_no_near_duplicate_points():
    # 8 个不同话题句子中选 5 条：任意两条入选要点的词集 Jaccard 不得过高（MMR 生效）
    r = _summarize_extractive(TITLE, CONTENT)
    sets = [set(extract_keywords(p, 20)) for p in r.key_points]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if union:
                assert len(sets[i] & sets[j]) / len(union) < 0.6


def test_empty_content_falls_back_to_title():
    r = _summarize_extractive(TITLE, "")
    assert TITLE in r.summary_short
    assert r.key_points == [TITLE]


def test_mmr_select_prefers_diversity():
    # 句 0/1 词面完全相同：选满 2 条时宁可取次优的句 2，也不选句 1 的重复内容
    scores = [0.9, 0.8, 0.5]
    word_sets = [{"苹果", "发布", "新机"}, {"苹果", "发布", "新机"}, {"芯片", "性能"}]
    picked = _mmr_select(scores, word_sets, k=2)
    assert picked == [0, 2]
