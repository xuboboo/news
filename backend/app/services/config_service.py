"""运行时配置服务：DB 覆盖（system_setting 表）优先，.env 为默认值。

管理面板修改后立即生效（API 与 Celery worker 每次调用都从 DB 读取），无需重建镜像。

校验分两层：
- 类型转换 _coerce：int/float/bool；bool 只接受明确的真假拼写，其余值一律拒绝
  （此前 "flase" 之类的手滑会被静默当作 false，曾可能悄悄关闭 cookie_secure）。
- 范围校验 CONFIG_RANGES：此前只存在于前端（Admin.tsx NUMERIC_RULES），
  拿到 token 的直连调用可写入 sim_threshold=-1 之类的破坏值，现前后端同规则。
"""
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.setting import SystemSetting
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

# 允许通过管理面板修改的配置项
ALLOWED_KEYS: dict[str, type] = {
    "llm_base_url": str,
    "llm_api_key": str,
    "llm_model": str,
    "embedding_base_url": str,
    "embedding_api_key": str,
    "embedding_model": str,
    "crawler_interval_minutes": int,
    # —— 互动数据轮询 ——
    "stats_poll_enabled": bool,
    "stats_poll_batch": int,
    "sim_threshold": float,
    "keyword_jaccard_threshold": float,
    "high_band_kw_floor": float,
    "cluster_lookback_hours": int,
    "cluster_window_hours": int,
    "admin_token": str,
    # —— 登录与会话 ——
    "session_ttl_hours": int,
    "session_idle_hours": int,
    "login_max_attempts": int,
    "login_lock_minutes": int,
    "login_ip_throttle_max": int,
    "login_ip_throttle_window_seconds": int,
    "cookie_secure": bool,
    "audit_retention_days": int,
    # —— 告警通知（alert_service 巡检推送；URL 留空 = 关闭） ——
    "alert_webhook_url": str,
    "alert_webhook_style": str,
    # —— 日报推送（report_push；总开关开 + 任一 URL 非空才推） ——
    "report_push_enabled": bool,
    "report_push_wecom_webhook_url": str,
    "report_push_feishu_webhook_url": str,
    "report_push_feishu_secret": str,
    # —— AI 成本治理（成本看板域） ——
    "ai_daily_cost_budget": float,
    "ai_usage_retention_days": int,
}

# 数值键的合法区间（min, max）：与前端 NUMERIC_RULES 保持一致。
# 下界取严防实际损害：保留期过短会被周清理一次性抹掉大量审计；
# IP 限速过紧会把整个出口 NAT 的正常用户挡在登录之外；空闲超时 0 = 关闭（允许）
CONFIG_RANGES: dict[str, tuple[int | float, int | float]] = {
    "crawler_interval_minutes": (1, 1440),
    "sim_threshold": (0, 1),
    "keyword_jaccard_threshold": (0, 1),
    "high_band_kw_floor": (0, 1),
    "cluster_lookback_hours": (1, 720),
    "cluster_window_hours": (1, 720),
    "session_ttl_hours": (1, 8760),
    "session_idle_hours": (0, 8760),
    "login_max_attempts": (1, 100),
    "login_lock_minutes": (1, 1440),
    "login_ip_throttle_max": (5, 10000),
    "login_ip_throttle_window_seconds": (60, 86400),
    "audit_retention_days": (30, 3650),
    "stats_poll_batch": (1, 50),
    # 预算 0 = 关闭告警；流水保留 0 = 永久保留（财务记录可全量归档不删）
    "ai_daily_cost_budget": (0, 100000),
    "ai_usage_retention_days": (0, 3650),
}

# 脱敏展示的字段（alert_webhook_url 通常内嵌钉钉/企微机器人 token）
SENSITIVE_KEYS = {
    "llm_api_key",
    "embedding_api_key",
    "admin_token",
    "alert_webhook_url",
    "report_push_wecom_webhook_url",
    "report_push_feishu_webhook_url",
    "report_push_feishu_secret",
}

_TRUE_SPELLINGS = {"1", "true", "yes", "on"}
_FALSE_SPELLINGS = {"0", "false", "no", "off"}

_WEBHOOK_STYLES = {"wecom", "dingtalk", "feishu", "raw"}


def _coerce(key: str, value: str):
    t = ALLOWED_KEYS[key]
    raw = str(value).strip()
    if t is int:
        n = int(float(raw))
        lo, hi = CONFIG_RANGES[key]
        if not (lo <= n <= hi):
            raise ValueError(f"{key} 需为 {lo} ~ {hi} 之间的整数")
        return str(n)
    if t is float:
        n = float(raw)
        lo, hi = CONFIG_RANGES[key]
        if not (lo <= n <= hi):
            raise ValueError(f"{key} 需为 {lo} ~ {hi} 之间的数值")
        return str(n)
    if t is bool:
        low = raw.lower()
        if low in _TRUE_SPELLINGS:
            return "true"
        if low in _FALSE_SPELLINGS:
            return "false"
        raise ValueError(f"{key} 仅接受 true/false（收到 {raw[:32]!r}）")
    return raw


def _validate_str(key: str, value: str) -> None:
    """字符串键的额外约束（类型转换之外）。"""
    if key == "alert_webhook_style" and value not in _WEBHOOK_STYLES:
        raise ValueError(f"alert_webhook_style 仅支持 {'/'.join(sorted(_WEBHOOK_STYLES))}")
    if key == "alert_webhook_url" and not value.lower().startswith(("http://", "https://")):
        raise ValueError("alert_webhook_url 需为 http(s):// 开头的完整地址")
    if key in ("report_push_wecom_webhook_url", "report_push_feishu_webhook_url") and not value.lower().startswith(
        ("http://", "https://")
    ):
        raise ValueError(f"{key} 需为 http(s):// 开头的完整机器人 webhook 地址")
    if key in (
        "alert_webhook_url",
        "report_push_wecom_webhook_url",
        "report_push_feishu_webhook_url",
    ):
        # 与 LLM/Embedding 地址同口径的 SSRF 防线：这些 URL 会被后端定时 POST
        # （告警每 5 分钟 / 日报每日），放行内网地址等于开放内网信标通道；
        # 全内网部署（自建网关）走既有 ALLOW_PRIVATE_AI_URLS 豁免
        try:
            assert_public_http_url(value)
        except UrlNotAllowed as exc:
            raise ValueError(str(exc))


def get_overrides(db: Session) -> dict[str, str]:
    rows = db.query(SystemSetting).all()
    return {r.key: r.value for r in rows}


def get_effective(db: Session, key: str):
    """单个配置项的生效值：DB 覆盖 > .env 默认。"""
    if key not in ALLOWED_KEYS:
        raise KeyError(key)
    row = db.get(SystemSetting, key)
    if row is not None and row.value != "":
        raw = row.value
    else:
        raw = str(getattr(get_settings(), key, ""))
    t = ALLOWED_KEYS[key]
    if t is int:
        return int(float(raw))
    if t is float:
        return float(raw)
    if t is bool:
        return raw.strip().lower() in _TRUE_SPELLINGS
    return raw


def effective_llm(db: Session) -> tuple[str, str, str]:
    """LLM (base_url, api_key, model) 生效配置；未配置返回空串三元组。"""
    return (
        get_effective(db, "llm_base_url"),
        get_effective(db, "llm_api_key"),
        get_effective(db, "llm_model"),
    )


def effective_embedding(db: Session) -> tuple[str, str, str]:
    return (
        get_effective(db, "embedding_base_url"),
        get_effective(db, "embedding_api_key"),
        get_effective(db, "embedding_model"),
    )


def set_overrides(db: Session, data: dict[str, str]) -> list[str]:
    """写入 DB 覆盖，返回成功处理的 key 列表；非法 key/值抛 ValueError。

    语义：值非空 → 写入覆盖（立即生效）；值为空串 → 删除覆盖（回落 .env 默认值）。
    空串判断必须先于类型转换——int('')/float('') 会抛错，导致数字键永远无法清除覆盖。
    """
    updated: list[str] = []
    for key, value in (data or {}).items():
        if key not in ALLOWED_KEYS:
            raise ValueError(f"unknown config key: {key}")
        raw = "" if value is None else str(value).strip()
        row = db.get(SystemSetting, key)
        if raw == "":
            if row is not None:
                db.delete(row)
                updated.append(key)
        else:
            clean = _coerce(key, raw)
            if ALLOWED_KEYS[key] is str:
                _validate_str(key, clean)
            if row is None:
                db.add(SystemSetting(key=key, value=clean))
            else:
                row.value = clean
            updated.append(key)
    db.commit()
    return updated


def masked_view(db: Session) -> dict:
    """管理面板展示：生效值 + 来源（env/db）+ 类型，敏感值脱敏展示。

    敏感键（api_key / admin_token / alert_webhook_url）的 raw 一律返回 None，明文不出后端：
    编辑语义由前端配合——留空 = 不修改，填新值 = 覆盖，显式清除 = 发空串回落 .env。
    type 字段供前端渲染控件（bool → 开关，其余 → 文本框）。
    """
    overrides = get_overrides(db)
    out = {}
    for key, key_type in ALLOWED_KEYS.items():
        default = str(getattr(get_settings(), key, ""))
        override = overrides.get(key, "")
        effective = override if override != "" else default
        if key_type is bool:
            # Settings 的 Python bool 经 str() 会变成 'True'/'False'；
            # 统一归一化为小写，前端开关/编辑比较都按 'true'/'false'
            effective = "true" if effective.strip().lower() in _TRUE_SPELLINGS else "false"
        source = "db" if override != "" else "env"
        if key in SENSITIVE_KEYS and effective:
            shown = effective[:4] + "****" + effective[-4:] if len(effective) > 8 else "****"
            raw = None  # 密钥明文不回传
        else:
            shown = effective
            raw = effective
        out[key] = {
            "value": shown,
            "raw": raw,
            "source": source,
            "overridden": override != "",
            "type": key_type.__name__,
        }
    return {"config": out, "allowlist": sorted(ALLOWED_KEYS), "sensitive": sorted(SENSITIVE_KEYS)}
