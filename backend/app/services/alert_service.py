"""轻量告警巡检：beat 每 5 分钟跑 alert_watch_task，异常状态经 webhook 推送。

检查项（与运维面板同口径，复用 infra_status）：
- Redis 连通、Celery worker 存活；
- 任务节奏心跳（event/crawler/stats/report 停摆；stats 被面板热停时豁免）；
- 最近失败任务未恢复。

推送策略（状态存 Redis key alert:watch）：
- ok → bad：立即推送；bad 持续期间每 30 分钟重复提醒一次；
- bad → ok：推送恢复通知；ok → ok：静默。
- alert_webhook_url 未配置（默认）= 告警关闭，只记日志。
- Redis 不可用时只记日志不推送——此时大概率连告警通道所需的队列都没了，
  每轮强行推送会造成重复轰炸。
"""
import logging
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services import config_service
from app.services.infra_status import cadence_warnings, celery_workers_ping, redis_client, redis_ping

logger = logging.getLogger(__name__)

_STATE_KEY = "alert:watch"
_RENOTIFY_AFTER_SECONDS = 30 * 60


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "无记录"
    return iso[:19].replace("T", " ") + " UTC"


def build_payload(style: str, text: str) -> dict:
    """按目标机器人的格式包装纯文本。style 由 config_service 校验，仅四种。"""
    if style == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if style == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if style == "raw":
        return {"title": "资讯雷达告警", "text": text}
    return {"msgtype": "text", "text": {"content": text}}  # 默认企业微信


def _collect_problems(db: Session) -> list[str]:
    settings = get_settings()
    problems: list[str] = []

    redis_ok, _ = redis_ping()
    if not redis_ok:
        problems.append("Redis 无法连接")

    workers, _err = celery_workers_ping()
    if not workers:
        problems.append("无在线 Celery Worker（事件聚类/AI 摘要/日报将停摆）")

    try:
        interval = config_service.get_effective(db, "crawler_interval_minutes")
    except Exception:
        interval = None
    try:
        stats_enabled = config_service.get_effective(db, "stats_poll_enabled")
    except Exception:
        stats_enabled = None
    for item in cadence_warnings(db, interval, skip={"stats"} if stats_enabled is False else set()):
        problems.append(
            f"任务停摆：{item['label']}（{item['name']}）最近运行 "
            f"{_fmt_dt(item['last_run_at'])}，距今 {item['age_minutes']} 分钟，超过阈值 "
            f"{item['threshold_minutes']} 分钟"
        )

    # 最近失败任务未恢复（与 admin_status 同口径：失败后有成功运行即视为恢复）
    from app.models.task_run import TaskRun

    last_failed = (
        db.query(TaskRun).filter(TaskRun.status == "failed").order_by(TaskRun.id.desc()).first()
    )
    if last_failed:
        recovered = (
            db.query(TaskRun)
            .filter(
                TaskRun.name == last_failed.name,
                TaskRun.status == "success",
                TaskRun.id > last_failed.id,
            )
            .count()
            > 0
        )
        if not recovered:
            problems.append(
                f"任务失败未恢复：{last_failed.name} 于 {_fmt_dt(last_failed.started_at.isoformat() if last_failed.started_at else None)}，"
                f"{(last_failed.detail or '')[:150]}"
            )

    return problems


def _push(url: str, style: str, text: str) -> None:
    resp = httpx.post(url, json=build_payload(style, text), timeout=10)
    if resp.status_code >= 300:
        raise RuntimeError(f"webhook 响应 HTTP {resp.status_code}：{resp.text[:200]}")


def _load_state() -> dict | None:
    """读告警状态机（Redis，共享连接池）。不可用返回 None（视为从未告警）。"""
    import json as _json

    try:
        raw = redis_client().get(_STATE_KEY)
        return _json.loads(raw) if raw else None
    except Exception:
        return None


def _save_state(state: dict) -> bool:
    import json as _json

    try:
        redis_client().set(_STATE_KEY, _json.dumps(state))
        return True
    except Exception:
        return False


def watch(db: Session) -> dict:
    """巡检一轮，返回 {problems, notified} 供任务结果/日志使用。"""
    settings = get_settings()
    problems = _collect_problems(db)
    now_bad = bool(problems)

    # 先判开关：告警关闭时直接返回（此时不该报 Redis 状态机的问题）
    try:
        url = config_service.get_effective(db, "alert_webhook_url")
        style = str(config_service.get_effective(db, "alert_webhook_style") or "wecom")
    except Exception as exc:
        logger.warning("alert config read failed: %s", exc)
        return {"problems": problems, "notified": False, "reason": "告警配置读取失败"}

    if not url:
        if now_bad:
            logger.warning("告警未推送（alert_webhook_url 未配置）：%s", "；".join(problems))
        return {"problems": problems, "notified": False, "reason": "alert_webhook_url 未配置"}

    if not redis_ping()[0]:
        # 状态机不可用（去重依赖 Redis）：此时多为 Redis 整体故障，强行推送会每轮重复轰炸——只记日志
        logger.warning("告警未推送（Redis 不可用，状态机停用）：%s", "；".join(problems))
        return {"problems": problems, "notified": False, "reason": "Redis 不可用"}

    state = _load_state()

    prev_bad = bool(state and state.get("bad"))
    last_notify = float(state.get("last_notify", 0)) if state else 0.0
    now_ts = time.time()

    should_notify = False
    kind = "info"
    if now_bad and not prev_bad:
        should_notify, kind = True, "故障"
    elif now_bad and prev_bad and now_ts - last_notify >= _RENOTIFY_AFTER_SECONDS:
        should_notify, kind = True, "持续告警"
    elif not now_bad and prev_bad:
        should_notify, kind = True, "恢复"

    notified = False
    if should_notify:
        app = settings.app_name
        text = (
            f"【{kind}】{app}\n"
            f"时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            + (
                "\n".join(f"· {p}" for p in problems)
                if problems
                else "全部检查项恢复正常。"
            )
        )
        try:
            _push(url, style, text)
            notified = True
        except Exception as exc:
            logger.error("告警 webhook 推送失败：%s", exc)

    # 无论推送成败都更新状态机（推送失败时下轮按「持续告警」节奏重试，不会每 5 分钟轰炸）
    _save_state({"bad": now_bad, "last_notify": now_ts if notified else last_notify})

    return {"problems": problems, "notified": notified}
