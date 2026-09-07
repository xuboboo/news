"""验收审计脚本：数据库级字段质量与流水线完整性检查。"""
from sqlalchemy import text

from app.core.db import new_session
from app.models.article import Article
from app.models.event import EventArticle, HotEvent
from app.models.summary import ArticleSummary

db = new_session()
print("== B3 字段质量 ==")
arts = db.query(Article).order_by(Article.id.desc()).limit(5).all()
for a in arts:
    print(f"id={a.id} nid={a.news_id} ch={a.channel} src={a.source} "
          f"content_len={len(a.content or '')} status={a.ai_status} "
          f"pub={a.publish_time.strftime('%m-%d %H:%M')}")
null_nid = db.query(Article).filter(Article.news_id.is_(None)).count()
pending = db.query(Article).filter(Article.ai_status == "pending").count()
empty_content = db.query(Article).filter((Article.content.is_(None)) | (Article.content == "")).count()
channels = dict(db.execute(text("SELECT channel, COUNT(*) FROM article GROUP BY channel")).all())
print(f"null_nid={null_nid} pending={pending} empty_content={empty_content}")
print(f"channel 分布: {channels}")

print("== C1 摘要质量抽查 ==")
samples = db.query(ArticleSummary).order_by(ArticleSummary.id.asc()).limit(3).all()
for s in samples:
    a = db.get(Article, s.article_id)
    print(f"[{s.model}] {a.title[:36]}")
    print(f"   一句话: {s.summary_short[:60]}")
    print(f"   要点数: {len(s.key_points or [])} | 标签: {s.tags}")
no_points = db.execute(text(
    "SELECT COUNT(*) FROM article_summary WHERE key_points IS NULL OR jsonb_array_length(key_points) = 0"
)).scalar()
no_tags = db.execute(text(
    "SELECT COUNT(*) FROM article_summary WHERE tags IS NULL OR jsonb_array_length(tags) = 0"
)).scalar()
print(f"空要点摘要数: {no_points} | 空标签摘要数: {no_tags}")

print("== C2 向量完整性 ==")
row = db.execute(text(
    "SELECT COUNT(*) FILTER (WHERE embedding IS NULL) AS no_vec, "
    "COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS has_vec, "
    "COUNT(DISTINCT vector_dims(embedding)) AS dims FROM article"
)).fetchone()
print(f"无向量: {row[0]} | 有向量: {row[1]} | 向量维度种类: {row[2]}")

print("== D 事件聚合完整性 ==")
evs = db.query(HotEvent).order_by(HotEvent.hot_score.desc()).limit(3).all()
for e in evs:
    links = db.query(EventArticle).filter_by(event_id=e.id).count()
    print(f"[{e.status}] 热度={e.hot_score:.1f} 成员={links} 时间线={len(e.timeline or [])} 质心={'有' if e.centroid is not None else '无'}")
    print(f"   标题: {e.title[:44]}")
    print(f"   总结: {(e.summary or '')[:70]}")
no_summary = db.query(HotEvent).filter((HotEvent.summary.is_(None)) | (HotEvent.summary == "")).count()
orphan_links = db.execute(text(
    "SELECT COUNT(*) FROM event_article ea LEFT JOIN article a ON a.id=ea.article_id WHERE a.id IS NULL"
)).scalar()
print(f"无总结事件: {no_summary} | 孤儿关联: {orphan_links}")
statuses = dict(db.execute(text("SELECT status, COUNT(*) FROM event GROUP BY status")).all())
print(f"状态分布: {statuses}")
