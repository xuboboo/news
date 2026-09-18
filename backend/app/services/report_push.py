"""早报 Webhook 推送：企业微信 + 飞书双通道机器人，markdown 早报卡片。

（原「软件日报」，2026-09-17 改版为「今日早报」：每日 08:00 推送昨日全天速览。）

配置三层（与全站配置语义一致）：运维面板 DB 覆盖 > .env 默认 > 关闭。
- report_push_enabled：总开关（面板开关 / env REPORT_PUSH_ENABLED）
- report_push_wecom_webhook_url：企业微信群机器人地址（留空 = 该通道关闭）
- report_push_feishu_webhook_url：飞书自定义机器人地址（留空 = 该通道关闭）
- report_push_feishu_secret：飞书机器人开启「签名校验」时的密钥（未开启留空）

两个通道可同时配置、同时推送。推送失败只记日志返回状态串，绝不影响早报生成本身。
"""
import base64
import hashlib
import hmac
import logging
import os
import re
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
    （内网自建网关用 AI_URL_ALLOWLIST 按主机名豁免，M-2）；不合格目标跳过并告警日志。
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
        logger.warning("早报推送目标未通过公网校验，已跳过：%s", exc)
        return False


def push_enabled(db: Session | None = None) -> bool:
    return bool(push_targets(db))


def _fmt_score(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return "0"


_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _weekday_cn(date_str: str) -> str:
    """早报刊头星期标注：解析失败返回空串（不阻断推送）。"""
    try:
        return _WEEKDAY_CN[datetime.fromisoformat(str(date_str)).weekday()]
    except ValueError:
        return ""


def _clip(text: object, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[: limit - 1] + "…" if len(s) > limit else s


def render_report_markdown(content: dict, provider: str = "wecom") -> str:
    """早报快照 → markdown 消息体。

    两通道 markdown 方言不同（官方文档核对 2026-09-11）：企微支持有限 HTML
    （<font color="comment">）；飞书卡片 markdown 不支持 HTML 标签，按原文显示，
    脚注须用纯文本。provider 决定脚注写法。

    结构（2026-09-17 早报改版）：刊头带星期 → 头条事件（含摘要）→ 其余热点。
    """
    date = content.get("date", "")
    weekday = _weekday_cn(date)
    head = f"华军软件园 · 今日早报 {date}" + (f"（{weekday}）" if weekday else "")
    lines: list[str] = [
        f"## {head} · 第 {content.get('issue_no', 1)} 期",
        "",
    ]
    overview = str(content.get("overview") or "").strip()
    if overview:
        source = content.get("overview_source")
        tag = "（AI 撰写）" if source == "llm" else ""
        lines += [f"{overview}{tag}", ""]
    total = content.get("articles_total")
    if total is not None:
        lines += [f"> 昨夜今晨共收录 **{total}** 篇报道", ""]
    events = content.get("top_events") or []
    if events:
        lead, rest = events[0], events[1:]
        lines += ["**【今日头条】**", ""]
        lines.append(
            f"**{lead.get('title', '')}**（热度 {_fmt_score(lead.get('hot_score'))}，"
            f"{lead.get('articles_count', 0)} 篇/{lead.get('sources_count', 0)} 源）"
        )
        summary = _clip(lead.get("summary"), 90)
        if summary:
            lines += ["", f"> {summary}"]
        lines.append("")
        if rest:
            lines += ["**更多热点**", ""]
            for i, e in enumerate(rest, 2):
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
            stamp = f"生成于 {dt.strftime('%Y-%m-%d %H:%M')} 北京时间"
            if provider == "feishu":
                lines += ["---", stamp]
            else:
                lines.append(f"<font color=\"comment\">{stamp}</font>")
        except ValueError:
            pass
    return "\n".join(lines)


def render_feishu_card(content: dict) -> dict:
    """早报快照 → 飞书结构化卡片 JSON（1.0 结构，纯函数供测试）。

    设计（2026-09-17 美化改版）：蓝色主题 header（副标题=日期·星期·期号）→
    综述导语 → 三栏数据行（收录/热点/来源）→ 分区 markdown（🔥今日头条/
    📈更多热点/🧭行业变化/⚡热点速览/📡重点来源）→ note 脚注。
    飞书卡片 markdown 不支持 HTML（<font> 等），脚注走 note 组件；
    整卡远小于自定义机器人 20KB 请求体上限。
    """
    date = content.get("date", "")
    weekday = _weekday_cn(date)
    issue = f"第 {content.get('issue_no', 1)} 期"
    subtitle = (date + (f"（{weekday}）" if weekday else "") + f" · {issue}") if date else issue

    elements: list[dict] = []
    overview = str(content.get("overview") or "").strip()
    if overview:
        if content.get("overview_source") == "llm":
            overview += "（AI 撰写）"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": overview}})

    total = content.get("articles_total")
    channels = content.get("channel_stats") or content.get("source_stats") or []
    if total is not None:
        stats = [(f"📰 **{total}**", "收录报道")]
        if content.get("top_events"):
            stats.append((f"🔥 **{len(content['top_events'])}**", "热点事件"))
        if channels:
            stats.append((f"📡 **{len(channels)}**", "来源栏目"))
        columns = [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": f"{num}\n{text}"}],
            }
            for num, text in stats
        ]
        elements.append({
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": "default",
            "columns": columns,
        })

    events = content.get("top_events") or []
    if events:
        lead, rest = events[0], events[1:]
        lead_md = (
            f"**{lead.get('title', '')}**（热度 {_fmt_score(lead.get('hot_score'))}，"
            f"{lead.get('articles_count', 0)} 篇/{lead.get('sources_count', 0)} 源）"
        )
        summary = _clip(lead.get("summary"), 90)
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": "**🔥 今日头条**\n" + lead_md + (f"\n{summary}" if summary else ""),
        })
        if rest:
            rest_lines = [
                f"{i}. **{e.get('title', '')}**（热度 {_fmt_score(e.get('hot_score'))}，"
                f"{e.get('articles_count', 0)} 篇/{e.get('sources_count', 0)} 源）"
                for i, e in enumerate(rest, 2)
            ]
            elements.append({
                "tag": "markdown",
                "content": "**📈 更多热点**\n" + "\n".join(rest_lines),
            })

    changes = content.get("industry_changes") or []
    if changes:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": "**🧭 行业变化**\n" + "\n".join(f"- {c}" for c in changes),
        })

    quick = content.get("quick_news") or []
    if quick:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": "**⚡ 热点速览**\n"
            + "\n".join(f"- {a.get('title', '')}（{a.get('source', '')}）" for a in quick),
        })

    if channels:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": "**📡 重点来源**\n"
            + "\n".join(
                f"- {c.get('channel') or c.get('source', '')}：{c.get('count', 0)} 篇"
                for c in channels
            ),
        })

    gen = content.get("generated_at")
    stamp = ""
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen)).astimezone(timezone(timedelta(hours=8)))
            stamp = f"生成于 {dt.strftime('%Y-%m-%d %H:%M')} 北京时间"
        except ValueError:
            pass
    footer = " · ".join(x for x in (stamp, "华军软件园资讯雷达自动生成") if x)
    if footer:
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"🤖 {footer}"}],
        })

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "华军软件园 · 今日早报"},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "elements": elements,
    }


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


def build_payload(provider: str, md: "str | dict", feishu_secret: str = "") -> dict:
    """按通道包装消息体（纯函数，供测试）。

    feishu 的第二参：传 dict = render_feishu_card 产出的完整卡片 JSON（推荐）；
    传 str = 兼容旧单 markdown 块结构。
    """
    if provider == "feishu":
        card: dict
        if isinstance(md, dict):
            card = md
        else:
            card = {
                "header": {
                    "title": {"tag": "plain_text", "content": "华军软件园 · 今日早报"},
                },
                "elements": [{"tag": "markdown", "content": md}],
            }
        body = {"msg_type": "interactive", "card": card}
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
    """推送早报到所有已配置通道。返回状态描述；未启用返回空串。"""
    targets = push_targets(db)
    if not targets:
        return ""
    results: list[str] = []
    for provider, url, secret in targets:
        if provider == "feishu":
            # 飞书走结构化卡片（header/分栏/分区/note）；卡片远小于 20KB 上限，不裁剪
            body_md: str | dict = render_feishu_card(content)
        else:
            body_md = clip_markdown_bytes(render_report_markdown(content, provider=provider))
        try:
            resp = httpx.post(url, json=build_payload(provider, body_md, secret), timeout=_PUSH_TIMEOUT)
            if resp.status_code == 200 and _provider_ok(provider, resp):
                results.append(f"{provider}:ok")
            else:
                results.append(f"{provider}:HTTP{resp.status_code}")
                logger.warning(
                    "早报 webhook 推送失败[%s]: HTTP %s %s",
                    provider, resp.status_code, resp.text[:200],
                )
        except Exception as exc:  # 网络/超时等一切异常都不上抛
            results.append(f"{provider}:fail:{exc.__class__.__name__}")
            logger.warning("早报 webhook 推送失败[%s]: %s", provider, exc)
    return "；".join(results)
