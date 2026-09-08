"""日报 Webhook 推送（企业微信群机器人 markdown 格式）。

配置走环境变量（配置面板白名单在 config_service，本期刻意不纠缠）：
- REPORT_PUSH_WEBHOOK_URL：企业微信群机器人 webhook 地址；空 = 推送关闭
- REPORT_PUSH_ENABLED：true 时日报生成后自动推送（默认 false，补算历史日报也会推）

推送失败只记日志返回错误描述，绝不影响日报生成本身。
"""
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("uvicorn.error")

_PUSH_TIMEOUT = 10.0


def push_enabled() -> bool:
    return bool(os.getenv("REPORT_PUSH_WEBHOOK_URL", "").strip()) and os.getenv(
        "REPORT_PUSH_ENABLED", "false"
    ).strip().lower() in ("1", "true", "yes", "on")


def _fmt_score(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return "0"


def render_report_markdown(content: dict) -> str:
    """日报快照 → 企业微信 markdown 消息体（纯函数，供测试）。"""
    date = content.get("date", "")
    lines: list[str] = [
        f"## 华军软件园 · 科技日报 {date}（第 {content.get('issue_no', 1)} 期）",
        "",
    ]
    overview = str(content.get("overview") or "").strip()
    if overview:
        lines += [overview, ""]
    total = content.get("articles_total")
    if total is not None:
        lines += [f"> 共收录 **{total}** 篇报道", ""]
    events = content.get("top_events") or []
    if events:
        lines += ["**当日 TOP 热点**", ""]
        for i, e in enumerate(events, 1):
            lines.append(
                f"{i}. **{e.get('title', '')}**（热度 {_fmt_score(e.get('hot_score'))}，"
                f"{e.get('articles_count', 0)} 篇/{e.get('sources_count', 0)} 源）"
            )
        lines.append("")
    changes = content.get("industry_changes") or []
    if changes:
        lines += ["**行业变化**", ""]
        lines += [f"- {c}" for c in changes]
        lines.append("")
    quick = content.get("quick_news") or []
    if quick:
        lines += ["**热点速览**", ""]
        for a in quick:
            lines.append(f"- {a.get('title', '')}（{a.get('source', '')}）")
        lines.append("")
    channels = content.get("channel_stats") or content.get("source_stats") or []
    if channels:
        lines += ["**重点来源**", ""]
        for c in channels:
            name = c.get("channel") or c.get("source", "")
            lines.append(f"- {name}：{c.get('count', 0)} 篇")
        lines.append("")
    gen = content.get("generated_at")
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen)).astimezone(timezone.utc)
            lines.append(f"<font color=\"comment\">生成于 {dt.strftime('%Y-%m-%d %H:%M')} UTC</font>")
        except ValueError:
            pass
    return "\n".join(lines)


def push_report(content: dict) -> str:
    """推送日报到企业微信群机器人。返回状态描述；未启用返回空串。"""
    url = os.getenv("REPORT_PUSH_WEBHOOK_URL", "").strip()
    if not url:
        return ""
    md = render_report_markdown(content)
    try:
        resp = httpx.post(
            url,
            json={"msgtype": "markdown", "markdown": {"content": md}},
            timeout=_PUSH_TIMEOUT,
        )
        if resp.status_code == 200:
            return "推送成功"
        return f"推送失败：HTTP {resp.status_code}"
    except Exception as exc:  # 网络/超时等一切异常都不上抛
        logger.warning("日报 webhook 推送失败: %s", exc)
        return f"推送失败：{exc.__class__.__name__}"
