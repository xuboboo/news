"""运行时配置服务：DB 覆盖（system_setting 表）优先，.env 为默认值。

管理面板修改后立即生效（API 与 Celery worker 每次调用都从 DB 读取），无需重建镜像。
"""
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.setting import SystemSetting

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
    "cluster_lookback_hours": int,
    "admin_token": str,
    # —— 登录与会话 ——
    "session_ttl_hours": int,
    "login_max_attempts": int,
    "login_lock_minutes": int,
    "cookie_secure": bool,
    "audit_retention_days": int,
}

# 脱敏展示的字段
SENSITIVE_KEYS = {"llm_api_key", "embedding_api_key", "admin_token"}

# 整数键下界：面板误设过小值会造成实际损害（如保留期 1 天 → 周清理一次性抹掉近半年审计）
MIN_INT_VALUES = {
    "audit_retention_days": 30,
}


def _coerce(key: str, value: str):
    t = ALLOWED_KEYS[key]
    if t is int:
        return str(int(str(value).strip()))
    if t is float:
        return str(float(str(value).strip()))
    if t is bool:
        return "true" if str(value).strip().lower() in ("1", "true", "yes", "on") else "false"
    return str(value).strip()


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
        return raw.strip().lower() in ("1", "true", "yes", "on")
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
            floor = MIN_INT_VALUES.get(key)
            if floor is not None and int(clean) < floor:
                raise ValueError(f"{key} 不能小于 {floor}（过低会造成不可逆的数据损失）")
            if row is None:
                db.add(SystemSetting(key=key, value=clean))
            else:
                row.value = clean
            updated.append(key)
    db.commit()
    return updated


def masked_view(db: Session) -> dict:
    """管理面板展示：生效值 + 来源（env/db），敏感值脱敏展示。

    敏感键（api_key / admin_token）的 raw 一律返回 None，明文不出后端：
    编辑语义由前端配合——留空 = 不修改，填新值 = 覆盖，显式清除 = 发空串回落 .env。
    """
    overrides = get_overrides(db)
    out = {}
    for key in ALLOWED_KEYS:
        default = str(getattr(get_settings(), key, ""))
        override = overrides.get(key, "")
        effective = override if override != "" else default
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
        }
    return {"config": out, "allowlist": sorted(ALLOWED_KEYS), "sensitive": sorted(SENSITIVE_KEYS)}
