"""审计服务：统一埋点入口 + 查询 + 导出 + 清理 + 哈希链校验。

覆盖面（action 清单）：登录成功/失败/锁定/登出/改密/会话吊销/越权访问、
租户与用户增改停、密码重置、运行时配置修改、LLM/Embedding 连通测试、
手动触发采集/聚合/日报。

防篡改：audit_log 逐行哈希链——row_hash = sha256(prev_hash ‖ 行内容规范化 JSON)，
行按 id 链接；写路径用 PostgreSQL advisory lock 串行化「读链尾 → 写入」，
避免并发写入分叉。哈希链是「篡改可发现」而非「篡改不可能」：能重写整表的攻击者
可以重建整条链，故校验脚本会打印链尾 hash，定期备份该值可获得更强的保证。
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)

# 动作清单：审计覆盖面的唯一权威定义（audit_auth.py 据此抽查）
ACTIONS = {
    # —— 认证与会话 ——
    "auth.login",
    "auth.login_failed",
    "auth.locked",
    "auth.throttled",
    "auth.logout",
    "auth.password_changed",
    "auth.session_revoked",
    "auth.access_denied",
    # —— 租户与用户管理 ——
    "tenant.created",
    "tenant.updated",
    "user.created",
    "user.updated",
    "user.disabled",
    "user.unlocked",
    "user.password_reset",
    # —— 运维管理（管理面板敏感操作）——
    "admin.config_updated",
    "admin.price_updated",
    "admin.test_llm",
    "admin.test_embedding",
    "admin.trigger_crawl",
    "admin.trigger_events",
    "admin.trigger_report",
    "admin.trigger_stats",
}

# 哈希链写路径的 pg advisory lock 键（任意常量，仅要求全库唯一）
_CHAIN_LOCK_KEY = 0x61756469  # b"audi"

# 越权留痕去重窗口：同 (操作者, 路径) 5 分钟内只记一条，防探测刷表
ACCESS_DENIED_DEDUP_SECONDS = 300

# 导出单次上限：防止失控查询拖垮库；超量请用时间范围分段导出
EXPORT_ROW_CAP = 100_000

EXPORT_ROW_CAP_REACHED = object()  # iter_logs 末尾哨兵：标记因上限截断


def client_ip(request: Request | None) -> str:
    """取可信客户端 IP。

    公网入口唯一经 nginx（compose 中后端仅绑回环）：nginx 已把 X-Real-IP 与
    X-Forwarded-For 覆写为 $remote_addr（见 frontend/nginx.conf），二者可信；
    本机直连后端调试时二者缺失，回落 TCP 对端地址。
    旧逻辑盲取 XFF 首段——nginx 追加语义下首段完全由客户端伪造，审计 IP
    全量失真（2026-09-09 审计修复，同款问题见 WordPress Stream 插件 CVE 披露）。
    """
    if request is None:
        return ""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real[:64]
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        # 最右段 = 最近一跳可信代理所见的地址；首段永远可被客户端伪造
        return xff.split(",")[-1].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def user_agent(request: Request | None) -> str:
    return (request.headers.get("user-agent", "") if request is not None else "")[:255]


def compute_row_hash(
    prev_hash: str,
    *,
    created_at: datetime | None,
    user_id: int | None,
    user_email: str,
    tenant_id: int | None,
    action: str,
    detail: dict | None,
    ip: str,
    user_agent: str,
) -> str:
    """行内容规范化 JSON 的 sha256；字段集变更需同步 audit_chain_verify 与存量校验口径。

    created_at 统一规范到 UTC 再序列化：同一时刻在不同会话时区下读回（如
    Asia/Shanghai）字符串不同，不规范化会导致写入与校验哈希必然失配。
    """
    if created_at is not None:
        created_at = (
            created_at.astimezone(timezone.utc)
            if created_at.tzinfo is not None
            else created_at.replace(tzinfo=timezone.utc)
        )
    payload = json.dumps(
        {
            "prev_hash": prev_hash or "",
            "created_at": created_at.isoformat() if created_at else "",
            "user_id": user_id,
            "user_email": user_email or "",
            "tenant_id": tenant_id,
            "action": action,
            "detail": detail or {},
            "ip": ip or "",
            "user_agent": user_agent or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record(
    db: Session,
    action: str,
    *,
    request: Request | None = None,
    user: User | None = None,
    user_id: int | None = None,
    user_email: str = "",
    tenant_id: int | None = None,
    detail: dict | None = None,
    commit: bool = True,
) -> None:
    """写一条审计日志（带防篡改哈希链）。

    传 user 对象或显式 user_id/user_email/tenant_id 之一均可。
    写失败不阻断业务流（OWASP：审计故障不应拖垮应用），但必须留错误日志；
    注意 commit=False 时失败同样会回滚会话——调用方的未提交业务变更一并丢弃，
    当前所有调用点均为「业务先提交、后记审计」模式，不受影响。
    """
    if user is not None:
        user_id = user.id
        user_email = user_email or user.email
        tenant_id = user.tenant_id
    try:
        # 链尾读取与写入同事务串行化，避免并发写取到同一 prev_hash 造成分叉。
        # advisory lock 是 PG 专有：测试用 SQLite 内存库跳过（单连接无并发分叉问题）
        if db.bind is None or db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY})
        prev_hash = (
            db.execute(text("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")).scalar()
            or ""
        )
        created_at = datetime.now(timezone.utc)
        ip_s = client_ip(request)
        ua_s = user_agent(request)
        detail_dict = detail or {}
        db.add(
            AuditLog(
                created_at=created_at,
                user_id=user_id,
                user_email=user_email or "",
                tenant_id=tenant_id,
                action=action,
                detail=detail_dict,
                ip=ip_s,
                user_agent=ua_s,
                prev_hash=prev_hash,
                row_hash=compute_row_hash(
                    prev_hash,
                    created_at=created_at,
                    user_id=user_id,
                    user_email=user_email,
                    tenant_id=tenant_id,
                    action=action,
                    detail=detail_dict,
                    ip=ip_s,
                    user_agent=ua_s,
                ),
            )
        )
        if commit:
            db.commit()
    except Exception:
        logger.exception("审计写入失败 action=%s actor=%s", action, user_email or user_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 会话已不可用，只能放弃
            pass


def record_access_denied(
    db: Session,
    *,
    request: Request,
    path: str,
    method: str,
    reason: str | None = None,
) -> None:
    """/api/v2/admin/* 的越权/无效令牌留痕：同 (操作者, 路径) 窗口内去重防刷表。"""
    from app.auth.deps import current_user_of

    user = current_user_of(request)
    user_email = (getattr(user, "email", "") or "") if user is not None else ""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ACCESS_DENIED_DEDUP_SECONDS)
    seen = (
        db.query(AuditLog.id)
        .filter(
            AuditLog.action == "auth.access_denied",
            AuditLog.user_email == user_email,
            AuditLog.detail["path"].astext == path,
            AuditLog.created_at >= cutoff,
        )
        .first()
    )
    if seen is not None:
        return
    detail: dict = {"path": path, "method": method}
    if reason:
        detail["reason"] = reason
    record(db, "auth.access_denied", request=request, user=user, detail=detail)


def parse_time_param(value: str | None) -> datetime | None:
    """查询参数时间解析：ISO 8601；无时区按 UTC（前端 datetime-local 已转 ISO+Z）。"""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _apply_filters(
    query,
    *,
    action: str | None,
    user_id: int | None,
    tenant_id: int | None,
    q: str | None,
    ip: str | None,
    time_from: datetime | None,
    time_to: datetime | None,
):
    if action:
        query = query.filter(AuditLog.action == action)
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if tenant_id is not None:
        query = query.filter(AuditLog.tenant_id == tenant_id)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(AuditLog.user_email.ilike(f"%{escaped}%", escape="\\"))
    if ip:
        # 前缀匹配：兼顾精确 IP 与 IPv4 网段排查
        escaped = ip.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(AuditLog.ip.like(f"{escaped}%", escape="\\"))
    if time_from is not None:
        query = query.filter(AuditLog.created_at >= time_from)
    if time_to is not None:
        query = query.filter(AuditLog.created_at <= time_to)
    return query


def query_logs(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 50,
    action: str | None = None,
    user_id: int | None = None,
    tenant_id: int | None = None,
    q: str | None = None,
    ip: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> dict:
    query = _apply_filters(
        db.query(AuditLog),
        action=action, user_id=user_id, tenant_id=tenant_id, q=q,
        ip=ip, time_from=time_from, time_to=time_to,
    )
    total = query.count()
    rows = (
        query.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_row_dict(r) for r in rows],
        "total": total,
    }


def iter_logs(
    db: Session,
    *,
    action: str | None = None,
    user_id: int | None = None,
    tenant_id: int | None = None,
    q: str | None = None,
    ip: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
):
    """导出迭代器：与 query_logs 同筛选，按 id 升序，上限 EXPORT_ROW_CAP。"""
    query = _apply_filters(
        db.query(AuditLog),
        action=action, user_id=user_id, tenant_id=tenant_id, q=q,
        ip=ip, time_from=time_from, time_to=time_to,
    )
    rows = query.order_by(AuditLog.id.asc()).limit(EXPORT_ROW_CAP + 1).yield_per(1000)
    count = 0
    for r in rows:
        count += 1
        if count > EXPORT_ROW_CAP:
            yield EXPORT_ROW_CAP_REACHED
            return
        yield r


def _row_dict(r: AuditLog) -> dict:
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "user_id": r.user_id,
        "user_email": r.user_email,
        "tenant_id": r.tenant_id,
        "action": r.action,
        "detail": r.detail or {},
        "ip": r.ip,
        "user_agent": r.user_agent,
    }


def verify_chain_rows(rows) -> list[str]:
    """校验按 id 升序的行序列，返回问题列表（空 = 通过）。

    - row_hash 为空的行视为链上线前的遗留段，跳过校验；
    - 链上第一行允许 prev_hash 指向不存在的行（前驱可能已被保留期清理），
      但内容哈希必须自洽；
    - 其余链上行必须 prev_hash 衔接前一链上行且内容哈希自洽——
      行内容被改 → 内容哈希不匹配；中间行被删/伪造插入 → 链断裂。
    """
    problems: list[str] = []
    prev = None
    for r in rows:
        if not getattr(r, "row_hash", ""):
            continue
        expect = compute_row_hash(
            r.prev_hash,
            created_at=r.created_at,
            user_id=r.user_id,
            user_email=r.user_email,
            tenant_id=r.tenant_id,
            action=r.action,
            detail=r.detail,
            ip=r.ip,
            user_agent=r.user_agent,
        )
        if r.row_hash != expect:
            problems.append(f"id={r.id} 内容哈希不匹配（行内容被修改）")
        if prev is not None and r.prev_hash != prev.row_hash:
            problems.append(f"id={r.id} 链断裂（与前一链上行不衔接，疑似删行/插行）")
        prev = r
    return problems


def purge_older_than(db: Session, days: int) -> int:
    """删除超过保留期的审计行（按 created_at 从头部清理，与哈希链校验兼容）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return (
        db.query(AuditLog).filter(AuditLog.created_at < cutoff).delete(synchronize_session=False)
    )
