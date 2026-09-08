"""聚类相似度标定报告（切换 Embedding 模型 / 本地哈希向量 v2 后使用）。

原理：以当前事件库的成员两两配对作为「同事件正例」，随机文章对作为「无关负例」，
统计当前向量空间下两组余弦相似度的分布，给出 sim_threshold 的建议值。
⚠️ 实测教训（2026-09，local-hash-256-v2 + IT之家语料）：同站科技新闻底噪很高
（随机对 p50≈0.78），建议值只是分位数起点——0.80 实测出现过合并（谷歌 Gemini
文章混入 Win11 灵动岛事件），0.85 精确率优先更稳。调低后务必人工抽查多成员
事件的成员一致性；误合并比碎片化更伤体验（增量聚类不会自动拆散错误合并，
需 TRUNCATE event/event_article 后重跑 repair_data.py 重建）。

用法（容器内）：python scripts/sim_report.py [--pairs 3000]
"""
import argparse
import math
import random
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.embedding import cosine
from app.core.db import SessionLocal
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.services import config_service

MAX_MEMBERS_PER_EVENT = 10  # 单事件最多取 10 个成员（45 对），防止超大事件刷屏
BUCKET = 0.1  # 分布直方图桶宽


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    vs = sorted(values)
    k = min(len(vs) - 1, max(0, int(round((len(vs) - 1) * p / 100))))
    return vs[k]


def _histogram(values: list[float]) -> str:
    bins = Counter(min(int(v / BUCKET), 10) for v in values)
    total = len(values) or 1
    return "  ".join(
        f"[{i * BUCKET:.1f}-{(i + 1) * BUCKET:.1f}):{bins.get(i, 0)}({bins.get(i, 0) * 100 / total:.0f}%)"
        for i in range(11)
        if bins.get(i, 0)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=3000, help="负例随机对数量上限")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        vecs = {
            a.id: list(a.embedding)
            for a in db.query(Article).filter(Article.embedding.isnot(None)).all()
        }
        print(f"已有向量文章：{len(vecs)} 篇")

        # —— 正例：事件成员两两配对 ——
        pos: list[float] = []
        events = db.query(HotEvent).all()
        for ev in events:
            aids = [
                l.article_id
                for l in db.query(EventArticle).filter_by(event_id=ev.id).all()
                if l.article_id in vecs
            ][:MAX_MEMBERS_PER_EVENT]
            for a, b in combinations(aids, 2):
                pos.append(cosine(vecs[a], vecs[b]))
        print(f"\n正例（同事件成员对，{len(events)} 个事件）：{len(pos)} 对")
        if pos:
            print(f"  p5={_pct(pos, 5):.3f}  p25={_pct(pos, 25):.3f}  p50={_pct(pos, 50):.3f}"
                  f"  p75={_pct(pos, 75):.3f}  p95={_pct(pos, 95):.3f}")
            print(f"  分布：{_histogram(pos)}")

        # —— 负例：随机文章对 ——
        ids = list(vecs)
        same_pairs: set[frozenset] = set()
        for ev in events:
            aids = [l.article_id for l in db.query(EventArticle).filter_by(event_id=ev.id).all()]
            for a, b in combinations(aids, 2):
                same_pairs.add(frozenset((a, b)))
        neg: list[float] = []
        attempts = 0
        while len(neg) < args.pairs and attempts < args.pairs * 10:
            attempts += 1
            a, b = random.sample(ids, 2)
            if frozenset((a, b)) in same_pairs:
                continue
            neg.append(cosine(vecs[a], vecs[b]))
        print(f"\n负例（随机文章对，已排除同事件）：{len(neg)} 对")
        if neg:
            print(f"  p50={_pct(neg, 50):.3f}  p95={_pct(neg, 95):.3f}  p99={_pct(neg, 99):.3f}")
            print(f"  分布：{_histogram(neg)}")

        # —— 当前生效阈值与建议 ——
        try:
            current = float(config_service.get_effective(db, "sim_threshold"))
        except Exception:
            current = float("nan")
        print(f"\n当前生效 sim_threshold：{current}")
        if pos and neg:
            suggestion = math.floor(_pct(pos, 10) / 0.05) * 0.05
            neg_p99 = _pct(neg, 99)
            print(f"建议起点（正例 p10 向下取 0.05）：{suggestion:.2f}")
            if suggestion < current:
                print(f"  → 正例大量低于当前阈值（疑似欠聚类/事件碎片化），"
                      f"建议在管理面板把 sim_threshold 调至 {suggestion:.2f} 后重聚类再观察")
            elif neg_p99 > current:
                print(f"  → 负例 p99={neg_p99:.3f} 越过当前阈值（疑似过聚类），建议适当调高")
            else:
                print("  → 当前阈值处于合理区间，无需调整")
    finally:
        db.close()


if __name__ == "__main__":
    main()
