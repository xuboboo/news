"""本地哈希向量 v2 单测：确定性 / 归一化 / 符号哈希 / sublinear TF / 相似度排序性质。"""
import math

from app.ai.embedding import LOCAL_EMBED_MODEL, _hash_vec, cosine

DIM = 256

_A = "苹果公司在库比蒂诺总部发布搭载M5芯片的新款MacBook Pro笔记本电脑"
_A2 = "苹果公司今日发布搭载M5芯片的新款MacBook Pro笔记本电脑"
_B = "研究人员在深海火山口发现新型嗜热细菌群落，刷新生命极限认知"


def test_deterministic():
    assert _hash_vec(_A, DIM) == _hash_vec(_A, DIM)


def test_l2_normalized():
    v = _hash_vec(_A, DIM)
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_self_cosine_is_one():
    v = _hash_vec(_A, DIM)
    assert math.isclose(cosine(v, v), 1.0, rel_tol=1e-9)


def test_signed_hash_produces_negative_components():
    # ±1 符号位使碰撞误差期望抵消：足够丰富的文本必然出现负分量
    v = _hash_vec(_A * 3 + _B, DIM)
    assert min(v) < 0


def test_sublinear_tf_repetition_not_proportional():
    # 同一文本重复 3 次仍高度相似（信号保留），但权重 1+ln(c) 与频次不成正比（cos<1）
    v1 = _hash_vec("苹果手机", DIM)
    v3 = _hash_vec("苹果手机苹果手机苹果手机", DIM)
    c = cosine(v1, v3)
    assert 0.9 < c < 1.0


def test_similarity_ordering():
    # 同事件改写文本的词面相似度必须显著高于无关文本
    va, va2, vb = _hash_vec(_A, DIM), _hash_vec(_A2, DIM), _hash_vec(_B, DIM)
    assert cosine(va, va2) > cosine(va, vb)
    assert cosine(va, va2) > 0.5
    assert cosine(va, vb) < 0.5


def test_local_model_name():
    assert LOCAL_EMBED_MODEL == "local-hash-256-v2"


# ———— 维度一致性守卫（2026-09-09 审计：zip 静默截断 → 显式拒绝） ————

def test_cosine_dimension_mismatch_raises():
    import pytest

    from app.ai.embedding import EmbeddingError, centroid

    with pytest.raises(EmbeddingError):
        cosine([1.0, 0.0], [1.0, 0.0, 0.0])
    with pytest.raises(EmbeddingError):
        centroid([[1.0, 0.0], [1.0, 0.0, 0.0]])
    # 空向量语义不变：返回 0 分 / 空质心，不抛
    assert cosine([], [1.0]) == 0.0
    assert centroid([]) == []
