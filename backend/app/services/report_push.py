"""日报 Webhook 推送：企业微信 + 飞书双通道机器人，markdown 日报卡片。

配置三层（与全站配置语义一致）：运维面板 DB 覆盖 > .env 默认 > 关闭。
- report_push_enabled：总开关（面板开关 / env REPORT_PUSH_ENABLED）
- report_push_wecom_webhook_url：企业微信群机器人地址（留空 = 该通道关闭）
- report_push_feishu_webhook_url：飞书自定义机器人地址（留空 = 该通道关闭）
- report_push_feishu_secret：飞书机器人开启「签名校验」时的密钥（未开启留空）

两个通道可同时配置、同时推送。推送失败只记日志返回状态串，绝不影响日报生成本身。
"""
import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.services import config_service
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

logger = logging.getLogger("uvicorn.error")

_PUSH_TIMEOUT = 10.0

_TRUE = ("1", "true", "yes", "on")


def _cfg(db: Session | None, key: str, env_key: str) -> str:
    """面板 DB 覆盖 > .env > 空串。DB 读取异常（未注册/连接问题）回落 env。"""
    if db is not None:
        try:
            v = config_service.get_effective(db, key)
            if v:
                return str(v).strip()
        except Exception:
            pass
    return os.getenv(env_key, "").strip()


def push_targets(db: Session | None) -> list[tuple[str, str, str]]:
    """当前启用的推送目标：[(provider, url, feishu_secret)]。总开关关或无 URL = 空。

    URL 在 config 写入时已过 SSRF 校验，这里对 env 直配/历史遗留值再复核一道
    （全内网部署沿用 ALLOW_PRIVATE_AI_URLS 豁免）；不合格目标跳过并告警日志。
    """
    enabled = _cfg(db, "report_push_enabled", "REPORT_PUSH_ENABLED").lower() in _TRUE
    if not enabled:
        return []
    out: list[tuple[str, str, str]] = []
    wecom = _cfg(db, "report_push_wecom_webhook_url", "REPORT_PUSH_WEBHOOK_URL")
    if wecom.lower().startswith(("http://", "https://")) and _public_url(wecom):
        out.append(("wecom", wecom, ""))
    feishu = _cfg(db, "report_push_feishu_webhook_url", "REPORT_PUSH_FEISHU_WEBHOOK_URL")
    if feishu.lower().startswith(("http://", "https://")) and _public_url(feishu):
        out.append(("feishu", feishu, _cfg(db, "report_push_feishu_secret", "REPORT_PUSH_FEISHU_SECRET")))
    return out


def _public_url(url: str) -> bool:
    try:
        assert_public_http_url(url)
        return True
    except UrlNotAllowed as exc:
        logger.warning("日报推送目标未通过公网校验，已跳过：%s", exc)
        return False


def push_enabled(db: Session | None = None) -> bool:
    return bool(push_targets(db))


def _fmt_score(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return "0"


def render_report_markdown(content: dict) -> str:
    """日报快照 → markdown 消息体（企业微信直接渲染；飞书卡片 markdown 元素渲染）。"""
    date = content.get("date", "")
    lines: list[str] = [
        f"## 华军软件园 · 软件日报 {date}（第 {content.get('issue_no', 1)} 期）",
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
            dt = datetime.fromisoformat(str(gen)).astimezone(timezone(timedelta(hours=8)))
            lines.append(f"<font color=\"comment\">生成于 {dt.strftime('%Y-%m-%d %H:%M')} 北京时间</font>")
        except ValueError:
            pass
    return "\n".join(lines)


# 企微 markdown 消息体上限 4096 字节，超限直接拒收（HTTP 200 + errcode 非 0）；
# 留出尾部标注与转义余量按 4000 字节预算在行界截断
_WECOM_MAX_BYTES = 4000
_TRUNCATION_SUFFIX = "\n…（内容过长，已截断）"


def clip_markdown_bytes(md: str, max_bytes: int = _WECOM_MAX_BYTES) -> str:
    """企微通道超长内容的守卫：字节预算内最后一个换行处截断，保条目结构完整。"""
    raw = md.encode("utf-8")
    if len(raw) <= max_bytes:
        return md
    budget = max_bytes - len(_TRUNCATION_SUFFIX.encode("utf-8"))
    clipped = raw[:budget].decode("utf-8", errors="ignore")
    nl = clipped.rfind("\n")
    if nl > 0:
        clipped = clipped[:nl]
    return clipped + _TRUNCATION_SUFFIX


def _feishu_sign(secret: str, timestamp: str) -> str:
    """飞书自定义机器人签名校验：key = "{timestamp}\\n{secret}"，HMAC-SHA256 后 base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def build_payload(provider: str, md: str, feishu_secret: str = "") -> dict:
    """按通道包装消息体（纯函数，供测试）。"""
    if provider == "feishu":
        body: dict = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "华军软件园 · 软件日报"},
                },
                "elements": [{"tag": "markdown", "content": md}],
            },
        }
        if feishu_secret:
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = _feishu_sign(feishu_secret, ts)
        return body
    return {"msgtype": "markdown", "markdown": {"content": md}}  # wecom


def _provider_ok(provider: str, resp: httpx.Response) -> bool:
    """HTTP 200 之外再校验机器人业务码：企微/飞书的签名错误、key 无效、内容超限、
    限流等业务失败几乎都以 200 + 错误码 body 返回，只看状态码会把失败记成 ok。
    另要求 body 呈机器人响应形状（有业务码键）——URL 误填到其他恰好返回 200 的
    服务（自建网关/探活端点）时这里判失败，测试推送才能暴露配置错误。"""
    try:
        body = resp.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    if provider == "wecom":
        # 企微机器人恒回 {"errcode": 0, "errmsg": "ok"}
        return body.get("errcode") == 0
    # 飞书：新版 {"code": 0}，旧版 {"StatusCode": 0}
    return body.get("code") == 0 or body.get("StatusCode") == 0


def push_report(db: Session | None, content: dict) -> str:
    """推送日报到所有已配置通道。返回状态描述；未启用返回空串。"""
    targets = push_targets(db)
    if not targets:
        return ""
    md = render_report_markdown(content)
    results: list[str] = []
    for provider, url, secret in targets:
        body_md = clip_markdown_bytes(md) if provider == "wecom" else md
        try:
            resp = httpx.post(url, json=build_payload(provider, body_md, secret), timeout=_PUSH_TIMEOUT)
            if resp.status_code == 200 and _provider_ok(provider, resp):
                results.append(f"{provider}:ok")
            else:
                results.append(f"{provider}:HTTP{resp.status_code}")
                logger.warning(
                    "日报 webhook 推送失败[%s]: HTTP %s %s",
                    provider, resp.status_code, resp.text[:200],
                )
        except Exception as exc:  # 网络/超时等一切异常都不上抛
            results.append(f"{provider}:fail:{exc.__class__.__name__}")
            logger.warning("日报 webhook 推送失败[%s]: %s", provider, exc)
    return "；".join(results)
