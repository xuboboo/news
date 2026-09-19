"""早报 Webhook 推送：企业微信 markdown 卡片 + 飞书文本早报，双通道机器人。

（原「软件日报」，2026-09-17 改版为「今日早报」；2026-09-19 飞书通道简化：
原结构化卡片信息密度过高，改为「问候语 + 早报速览 10 条」纯文本速览。）

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
from app.utils.text import strip_media_lede
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


def render_report_markdown(content: dict) -> str:
    """早报快照 → 企微 markdown 消息体（2026-09-19 起仅企微通道使用；飞书通道走
    render_feishu_text 纯文本速览，本函数不再有 provider 方言分支）。

    企微 markdown 支持有限 HTML（<font color="comment">，官方文档核对 2026-09-11），
    脚注用它弱化展示。

    结构（2026-09-17 早报改版）：刊头带星期 → 头条事件（含摘要）→ 其余热点。
    2026-09-19 按需求移除「重点来源」频道收录统计（不展示收录篇数）；
    头条摘要字数与飞书通道同步放宽 90 → 120（用户反馈截断读不完整）。
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
    events = content.get("top_events") or []
    if events:
        lead, rest = events[0], events[1:]
        lines += ["**【今日头条】**", ""]
        lines.append(
            f"**{lead.get('title', '')}**（热度 {_fmt_score(lead.get('hot_score'))}，"
            f"{lead.get('articles_count', 0)} 篇/{lead.get('sources_count', 0)} 源）"
        )
        summary = _clip(lead.get("summary"), 120)
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
    gen = content.get("generated_at")
    if gen:
        try:
            dt = datetime.fromisoformat(str(gen)).astimezone(timezone(timedelta(hours=8)))
            stamp = f"生成于 {dt.strftime('%Y-%m-%d %H:%M')} 北京时间"
            lines.append(f"<font color=\"comment\">{stamp}</font>")
        except ValueError:
            pass
    return "\n".join(lines)


# 飞书速览带圈序号（速览上限 10 条，恰用到 ❿；超出防御性退化为数字点）
_CIRCLED_NUMS = "❶❷❸❹❺❻❼❽❾❿"
_SENTENCE_END_RE = re.compile(r"[。！？;；]")
# 转述壳「科技媒体 X（今天）发布博文，/博主 X 指出，」：事实本体在连接词之后。
# 来源词必选——防误切「微软表示，…」这类主语+谓语的正常句（无壳）；
# 覆盖「据报道称，/博文称，」等壳切断后的句首残留
_LEAD_SHELL_RE = re.compile(
    r"^(?:科技媒体|外媒|韩媒|美媒|日媒|博主|消息人士|研究机构|分析机构|评测机构)"
    r"[^，。]{0,40}?(?:发布博文|发布视频|报道|指出|表示|透露|分享)[，,：:]\s*"
)
_LEAD_RESIDUAL_RE = re.compile(r"^(?:据报道称|据报道|报道称|博文称|视频称|报告中称)[，,：:]?\s*")


def _strip_lead(s: str) -> str:
    """剥离开头转述壳，返回事实本体。

    壳命中但剩余不足 8 字（壳占满了 44 字摘要，本体被截没）→ 返回空串
    放弃说明，条目只保留标题——比整条搬导语壳干净。
    """
    m = _LEAD_SHELL_RE.match(s)
    if not m:
        return s
    rest = _LEAD_RESIDUAL_RE.sub("", s[m.end():].lstrip("，,：: "))
    return rest if len(rest) >= 8 else ""


def _brief_note(raw: object, limit: int = 120) -> str:
    """速览条目的一句说明：剥媒体导语壳/转述壳 → 取首句去尾句号 → 超限加省略号。

    快照里的 summary / summary_short 常以原文导语开头（"IT之家 9 月 18 日消息，
    科技媒体……发布博文，报道称……"），整句搬进速览会每条同款开头且挤占字数。
    字数上限 2026-09-19 由 44 放宽到 120（用户反馈摘要截得太短、读起来不完整；
    飞书 text 上限充裕，10 条 × 120 字仍远小于上限）。
    """
    s = strip_media_lede(re.sub(r"\s+", " ", str(raw or "")).strip())
    if not s:
        return ""
    s = _strip_lead(s)
    m = _SENTENCE_END_RE.search(s)
    if m and m.end() <= limit + 4:
        return s[: m.end()].rstrip("。！？;；")
    return _clip(s.rstrip("。！？;；"), limit)


def render_feishu_text(content: dict, limit: int = 10) -> str:
    """早报快照 → 飞书文本早报（纯函数供测试）。

    2026-09-19 两轮改版：简化为文本速览后，晚间再扩到 10 条（❶~❿ 与热榜
    TOP10 对齐）并重排版——刊头（问候 + 日期星期 + 引导句）→ 空行 →
    ❶~❿ 逐条「标题：一句说明」，每条之间空一行（2026-09-19 按用户要求
    加入，提升逐条可读性）→ 空行 → 分隔线 + 品牌·期号落款。

    条目取材：TOP 事件（标题 + 摘要首句）在前，热点速览文章补足；仍不足
    limit 条时以热榜 TOP10 增量补位（剔除 TOP 事件成员，防同一资讯两遍）。
    """
    items: list[tuple[str, str]] = []
    seen_titles: set[str] = set()

    def _add(title: object, note: str) -> None:
        t = re.sub(r"\s+", " ", str(title or "")).strip()
        if not t or t in seen_titles:
            return
        seen_titles.add(t)
        items.append((t, note))

    for e in content.get("top_events") or []:
        note = _brief_note(e.get("summary"))
        if note.startswith("围绕「"):
            # 事件 summary 常为「围绕「标题」共有 N 篇…」的模板句（标题复读、
            # 无事实增量）：说明退化为结构化计数；单来源连计数也无信息量 → 只留标题
            n, m = e.get("articles_count") or 0, e.get("sources_count") or 0
            note = (
                f"聚合 {n} 篇" + (f"，{m} 家来源交叉报道" if m and m > 1 else "")
                if n > 1 else ""
            )
        _add(e.get("title"), note)
    for a in content.get("quick_news") or []:
        _add(a.get("title"), _brief_note(a.get("summary_short")))
    if len(items) < limit:
        featured = set(content.get("featured_article_ids") or [])
        for a in content.get("top_articles") or []:
            if len(items) >= limit:
                break
            if a.get("id") in featured:
                continue
            _add(a.get("title"), _brief_note(a.get("summary_short")))

    lines: list[str] = ["🌅 早报时间，大家好"]
    date = str(content.get("date") or "")
    weekday = _weekday_cn(date)
    try:
        d = datetime.fromisoformat(date)
        lines.append(f"现在是 {d.year} 年 {d.month} 月 {d.day} 日" + (f" 星期{weekday[1]}" if weekday else ""))
    except ValueError:
        pass
    lines += ["今天的重要科技资讯：", ""]
    briefs: list[str] = []
    for i, (title, note) in enumerate(items[:limit]):
        num = _CIRCLED_NUMS[i] if i < len(_CIRCLED_NUMS) else f"{i + 1}."
        briefs.append(f"{num} {title}：{note}" if note else f"{num} {title}")
    lines.append("\n\n".join(briefs))  # 每条之间空一行
    issue = content.get("issue_no")
    lines += ["", "————————————", f"华军软件园 · 今日早报{f' 第 {issue} 期' if issue else ''}"]
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


def build_payload(provider: str, body_text: str, feishu_secret: str = "") -> dict:
    """按通道包装消息体（纯函数，供测试）。

    飞书通道（2026-09-19 简化改版）：text 消息承载「问候语 + 早报速览 10 条」
    文本；签名校验对 text 消息同样生效（timestamp/sign 是机器人级配置）。
    """
    if provider == "feishu":
        body = {"msg_type": "text", "content": {"text": body_text}}
        if feishu_secret:
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = _feishu_sign(feishu_secret, ts)
        return body
    return {"msgtype": "markdown", "markdown": {"content": body_text}}  # wecom


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
            # 飞书走文本早报（问候语 + 速览 10 条）；远小于 20KB 上限，不裁剪
            body_text: str = render_feishu_text(content)
        else:
            body_text = clip_markdown_bytes(render_report_markdown(content))
        try:
            resp = httpx.post(url, json=build_payload(provider, body_text, secret), timeout=_PUSH_TIMEOUT)
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
