"""审计服务单测：IP 提取可信度、行哈希稳定性、哈希链校验（纯函数，不依赖 DB）。

运行方式（backend/ 下）：python -m pytest tests/test_audit_service.py -q
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.audit_service import client_ip, compute_row_hash, verify_chain_rows


def _request(headers=None, host="203.0.113.9"):
    """模拟 Starlette Request 的 client_ip 所依赖的最小接口。"""
    return SimpleNamespace(
        headers={k.lower(): v for k, v in (headers or {}).items()},
        client=SimpleNamespace(host=host),
    )


# ———— client_ip：可信度优先级 ————

def test_client_ip_none_request():
    assert client_ip(None) == ""


def test_client_ip_prefers_x_real_ip():
    """nginx 覆写的 X-Real-IP 最可信：XFF 首段伪造值不得生效。"""
    r = _request({"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
    assert client_ip(r) == "198.51.100.7"


def test_client_ip_xff_takes_rightmost():
    """无 X-Real-IP 时取 XFF 最右段（最近一跳可信代理所见），而非可伪造的首段。"""
    r = _request({"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
    assert client_ip(r) == "203.0.113.9"


def test_client_ip_xff_single():
    assert client_ip(_request({"X-Forwarded-For": "198.51.100.7"})) == "198.51.100.7"


def test_client_ip_falls_back_to_socket():
    assert client_ip(_request()) == "203.0.113.9"


def test_client_ip_truncates_long():
    r = _request({"X-Real-IP": "a" * 100})
    assert len(client_ip(r)) == 64


def test_client_ip_blank_headers_fall_through():
    """头存在但为空：应继续回落，不得返回空串掩盖真实来源。"""
    r = _request({"X-Real-IP": "", "X-Forwarded-For": "  "})
    assert client_ip(r) == "203.0.113.9"


# ———— compute_row_hash ————

def _hash_args(**overrides):
    base = dict(
        created_at=datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc),
        user_id=1,
        user_email="a@b.c",
        tenant_id=2,
        action="auth.login",
        detail={"session_id": 3},
        ip="198.51.100.7",
        user_agent="pytest",
    )
    base.update(overrides)
    return base


def test_row_hash_deterministic():
    assert compute_row_hash("p", **_hash_args()) == compute_row_hash("p", **_hash_args())


def test_row_hash_prev_affects_result():
    """prev_hash 参与哈希：不同前驱必须产出不同行哈希（链不可拼接）。"""
    h1 = compute_row_hash("prev-a", **_hash_args())
    h2 = compute_row_hash("prev-b", **_hash_args())
    assert h1 != h2


def test_row_hash_content_sensitive():
    h1 = compute_row_hash("p", **_hash_args())
    h2 = compute_row_hash("p", **_hash_args(ip="203.0.113.9"))
    assert h1 != h2


# ———— verify_chain_rows ————

class Row(SimpleNamespace):
    """模拟 AuditLog 行。"""


def _chain(n=4):
    """构造 n 行合法链；返回行列表。"""
    rows, prev = [], ""
    for i in range(n):
        row = Row(
            id=i + 1,
            created_at=datetime(2026, 9, 9, 12, i, 0, tzinfo=timezone.utc),
            user_id=1, user_email="a@b.c", tenant_id=2,
            action="auth.login", detail={}, ip="192.0.2.1", user_agent="pytest",
            prev_hash=prev, row_hash="",
        )
        row.row_hash = compute_row_hash(
            prev, created_at=row.created_at, user_id=row.user_id,
            user_email=row.user_email, tenant_id=row.tenant_id,
            action=row.action, detail=row.detail, ip=row.ip, user_agent=row.user_agent,
        )
        rows.append(row)
        prev = row.row_hash
    return rows


def test_verify_chain_passes():
    assert verify_chain_rows(_chain()) == []


def test_verify_chain_skips_legacy_rows():
    """row_hash 为空的存量遗留段：跳过校验，不得误报。"""
    rows = [Row(id=0, row_hash="", prev_hash="")] + _chain()
    assert verify_chain_rows(rows) == []


def test_verify_chain_detects_content_tamper():
    rows = _chain()
    rows[2].ip = "6.6.6.6"  # 行内容被改
    problems = verify_chain_rows(rows)
    assert problems and "id=3" in problems[0]


def test_verify_chain_detects_middle_deletion():
    rows = _chain()
    del rows[1]  # 中间行被删除 → 后继 prev_hash 断链
    problems = verify_chain_rows(rows)
    assert problems and "链断裂" in "\n".join(problems)


def test_verify_chain_detects_inserted_row():
    rows = _chain()
    forged = Row(
        id=99, created_at=rows[-1].created_at, user_id=1, user_email="a@b.c",
        tenant_id=2, action="auth.login", detail={}, ip="192.0.2.1",
        user_agent="pytest", prev_hash="fabricated", row_hash="fabricated",
    )
    problems = verify_chain_rows(rows + [forged])
    assert problems


def test_verify_chain_head_survives_purge():
    """保留期清理删掉链首后，剩余首行 prev_hash 指向不存在的行——不得误报。"""
    rows = _chain()
    del rows[0]
    assert verify_chain_rows(rows) == []


def test_row_hash_tz_normalization():
    """写入用 UTC、读回带 +08:00（PG 会话时区）：同一时刻必须产出同一哈希。"""
    from datetime import timedelta

    utc_dt = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    sh_dt = utc_dt.astimezone(timezone(timedelta(hours=8)))
    args = _hash_args()
    args.pop("created_at")
    h_utc = compute_row_hash("p", created_at=utc_dt, **args)
    h_sh = compute_row_hash("p", created_at=sh_dt, **args)
    assert h_utc == h_sh


def test_verify_chain_survives_session_timezone_readback():
    """链按 UTC 哈希写入，读回行带 +08:00 时区：校验不得误报内容篡改。"""
    from datetime import timedelta

    rows, prev = [], ""
    for i in range(3):
        utc_dt = datetime(2026, 9, 9, 4, i, 0, tzinfo=timezone.utc)
        row = Row(
            id=i + 1,
            # 模拟 PG 会话时区 Asia/Shanghai 的读回表示
            created_at=utc_dt.astimezone(timezone(timedelta(hours=8))),
            user_id=1, user_email="a@b.c", tenant_id=2,
            action="auth.login", detail={}, ip="192.0.2.1", user_agent="pytest",
            prev_hash=prev, row_hash="",
        )
        # 写入时刻以 UTC 表示计算哈希（record 的行为）
        row.row_hash = compute_row_hash(
            prev, created_at=utc_dt, user_id=row.user_id,
            user_email=row.user_email, tenant_id=row.tenant_id,
            action=row.action, detail=row.detail, ip=row.ip, user_agent=row.user_agent,
        )
        rows.append(row)
        prev = row.row_hash
    assert verify_chain_rows(rows) == []
