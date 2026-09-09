"""验收审计脚本：数据库级字段质量与流水线完整性检查。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 支持 python scripts/audit_db.py 直接运行

from sqlalchemy import text

from app.core.db import new_session
from app.models.article import Article
from app.models.audit_log import AuditLog
from app.models.event import EventArticle, HotEvent
from app.models.summary import ArticleSummary
from app.models.tenant import Tenant
from app.models.user import User, ROLE_SUPER_ADMIN
from app.models.user_session import UserSession

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


print("== G 认证与多租户完整性 ==")

from datetime import datetime, timezone  # noqa: E402 脚本分节执行，置于此节使用处之前

_now = datetime.now(timezone.utc)
tenants = db.query(Tenant).all()
print(f"租户: {len(tenants)} 个 · " + " / ".join(f"{t.name}({t.status},{t.user_count if hasattr(t,'user_count') else db.query(User).filter_by(tenant_id=t.id).count()})" for t in tenants))

users = db.query(User).all()
role_dist = {}
status_dist = {}
for u in users:
    role_dist[u.role] = role_dist.get(u.role, 0) + 1
    status_dist[u.status] = status_dist.get(u.status, 0) + 1
print(f"用户: {len(users)} 个 · 角色 {role_dist} · 状态 {status_dist}")

upper_emails = [u.email for u in users if u.email != u.email.lower()]
print(f"邮箱未小写归一: {len(upper_emails)} {upper_emails[:3]}")
active_supers = db.query(User).filter(User.role == ROLE_SUPER_ADMIN, User.status == "active").count()
print(f"启用中的超管: {active_supers}（平台必须 ≥ 1）")

active_sessions = db.query(UserSession).filter(
    UserSession.revoked_at.is_(None), UserSession.expires_at > _now
).count()
expired_uncleaned = db.query(UserSession).filter(UserSession.expires_at <= _now).count()
orphan_sessions = db.execute(text(
    "SELECT COUNT(*) FROM user_session s LEFT JOIN app_user u ON u.id = s.user_id WHERE u.id IS NULL"
)).scalar()
disabled_user_sessions = db.execute(text(
    "SELECT COUNT(*) FROM user_session s JOIN app_user u ON u.id = s.user_id "
    "WHERE s.revoked_at IS NULL AND s.expires_at > :now AND u.status <> 'active'"
), {"now": _now}).scalar()
print(f"活跃会话: {active_sessions} | 过期待清理: {expired_uncleaned} | 孤儿会话: {orphan_sessions} | 停用/异常账号残留会话: {disabled_user_sessions}")

must_change_with_sessions = db.execute(text(
    "SELECT COUNT(DISTINCT s.user_id) FROM user_session s JOIN app_user u ON u.id = s.user_id "
    "WHERE s.revoked_at IS NULL AND s.expires_at > :now AND u.must_change_password"
), {"now": _now}).scalar()
print(f"首登未改密但持有会话的账号: {must_change_with_sessions}（服务端守卫应已拦截其业务请求）")

audit_total = db.query(AuditLog).count()
actions = dict(db.execute(text("SELECT action, COUNT(*) FROM audit_log GROUP BY action ORDER BY COUNT(*) DESC")).all())
print(f"审计日志: {audit_total} 条 · 动作分布: {actions}")
no_ip = db.query(AuditLog).filter(AuditLog.ip == "").count()
print(f"缺 IP 的审计行: {no_ip}（ Celery/内部写入允许为空，HTTP 请求不应为空）")

stats_polled = db.query(Article).filter(Article.stats_polled_at.isnot(None)).count()
give_up = db.query(Article).filter(Article.stats_fail_count >= 6).count()
print(f"互动数据: 已轮询 {stats_polled} 篇 · 放弃终态 {give_up} 篇")
