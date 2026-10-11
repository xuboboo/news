"""网站资讯稿的确定性质量检查。

这不是所谓的 AI 检测器，也不把某个词出现一次就判定为低质。它只拦截
影响事实性、可读性、搜索摘要质量和来源增值的可观察问题，并把问题反馈给
同一模型做一次有针对性的重写。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


_HEADING_RE = re.compile(r"^[ \t]{0,3}#{2,6}[ \t]+(.+?)\s*$", re.M)
_GENERIC_HEADING_RE = re.compile(
    r"^(?:事件概述|事件简介|背景介绍|影响分析|总结|结语|相关信息|详细内容|具体情况|写在最后)$",
    re.I,
)
_AI_STYLE_PATTERNS = (
    re.compile(r"值得注意的是"),
    re.compile(r"与此同时"),
    re.compile(r"总的来说|综上所述|整体来看"),
    re.compile(r"不难发现"),
    re.compile(r"这意味着"),
    re.compile(r"可以看出"),
    re.compile(r"换言之|换句话说"),
    re.compile(r"在.{0,12}背景下"),
    re.compile(r"随着.{0,16}(?:不断|持续|快速)"),
    re.compile(r"不仅.{0,30}而且"),
    re.compile(r"一方面.{0,30}另一方面"),
    re.compile(r"本文将|带你了解|下面我们|接下来将"),
)
_JUDGEMENT_DEFERRAL_RE = re.compile(r"(?:读者|用户)?(?:可以|可)?(?:结合[^。]{0,12})?自行判断|尚无定论|见仁见智")


def _plain(text: object) -> str:
    value = str(text or "")
    value = re.sub(r"```.*?```", "", value, flags=re.S)
    value = re.sub(r"!?(?:\[[^\]]*\])\([^)]*\)", "", value)
    value = re.sub(r"[#>*_`\[\],，。、！？；：:（）()「」“”\"'-]", "", value)
    return re.sub(r"\s+", "", value)


def _paragraphs(content: str) -> list[str]:
    result: list[str] = []
    for block in re.split(r"\n\s*\n", content or ""):
        block = block.strip()
        if not block or block.startswith("#") or block.startswith("["):
            continue
        if block.startswith("-"):
            continue
        result.append(block)
    return result


def _terms(value: object) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,，、\n]+", str(value or ""))
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def inspect_site_article(
    data: dict[str, Any],
    material: list[dict],
    brief: dict[str, Any] | None = None,
    tier: str = "brief",
) -> list[dict[str, str]]:
    """检查网站稿，不修改模型输出。返回按严重程度排序的问题。"""
    issues: list[dict[str, str]] = []
    title = str(data.get("title") or "").strip()
    digest = str(data.get("content_intro") or "").strip()
    content = str(data.get("content_md") or "").strip()
    plain = _plain(content)
    paragraphs = _paragraphs(content)
    headings = _HEADING_RE.findall(content)

    if not title or not content:
        return [_issue("missing_core", "error", "标题或正文为空，不能发布")]
    if len(title) > 80:
        issues.append(_issue("title_long", "error", "标题超过网站工具上限 80 字"))
    if not digest:
        issues.append(_issue("missing_intro", "error", "内容简介为空"))
    if len(digest) > 160:
        issues.append(_issue("intro_long", "error", "内容简介超过 160 字"))
    soft_min = 700 if tier == "depth" else 430
    hard_min = 480 if tier == "depth" else 300
    if len(plain) < hard_min:
        issues.append(_issue("too_thin", "error", "正文信息量过薄，像摘要扩写而不是完整资讯稿"))
    elif len(plain) < soft_min:
        issues.append(_issue("too_thin", "warning", "正文偏短；素材不足时可以保持简洁，但不要用空话补长度"))

    expected_headings = (3, 6) if tier == "depth" else (2, 4)
    if not expected_headings[0] <= len(headings) <= expected_headings[1]:
        issues.append(_issue(
            "heading_shape", "error",
            f"正文需要 {expected_headings[0]}~{expected_headings[1]} 个有信息量的小标题，当前为 {len(headings)} 个",
        ))
    generic_headings = [h.strip() for h in headings if _GENERIC_HEADING_RE.fullmatch(h.strip())]
    if generic_headings:
        issues.append(_issue("generic_heading", "error", "小标题过于模板化：" + "、".join(generic_headings)))
    # 百度内容诊断把"自问自答式小标题"列为 AI 痕迹与低质特征：问句式小标题至多 1 个
    question_headings = [h for h in headings if h.strip().endswith(("？", "?"))]
    if len(question_headings) > 1:
        issues.append(_issue(
            "heading_question_overload", "warning",
            f"问句式小标题 {len(question_headings)} 个，超过 1 个会显得像自问自答，其余改成信息陈述式",
        ))
    if re.search(r"^#\s+", content, re.M):
        issues.append(_issue("duplicate_h1", "error", "正文包含一级标题，可能与 CMS 独立标题重复"))

    if re.search(r"(?:本文将|本文主要|带你了解|在本文中|下面我们|接下来我们)", plain):
        issues.append(_issue("meta_talk", "error", "正文出现面向模型/文章流程的套话"))
    # 写作提示词要求正文不出现来源编号和参考来源小节，服务端会按实际素材补来源。
    # 模型偶尔仍把简报编号、尾注或带编号的来源括号写进正文，这类内容会直接外泄到 CMS。
    if re.search(r"(?<![A-Za-z0-9])R\d{1,2}(?![A-Za-z0-9])", plain):
        issues.append(_issue("source_id_leak", "error", "正文出现内部来源编号（R1/R2 等），不能发布"))
    if re.search(r"(?:参考来源|资料来源|本文参考|据.{0,8}等\d{1,2}家媒体)", plain):
        issues.append(_issue("source_block_leak", "error", "正文出现参考来源/资料出处表述，服务端会统一补来源"))
    # 百度自检 B3：可被搜索摘要引用的具体数据点（版本号/日期/数量）是收录与引用的基础
    if tier in ("brief", "depth") and not re.search(r"\d", plain):
        issues.append(_issue("no_quotable_data", "warning", "正文没有任何数字、版本号或日期，缺少可被搜索引用的具体数据点"))
    if _JUDGEMENT_DEFERRAL_RE.search(plain):
        issues.append(_issue("judgment_deferral", "warning", "出现把判断推给读者的表述（自行判断/尚无定论），有结论直说，事实未知就写“目前未知”"))
    style_hits = sum(len(pattern.findall(plain)) for pattern in _AI_STYLE_PATTERNS)
    if style_hits >= 4:
        issues.append(_issue("ai_cadence", "error", f"模板化连接和抽象收束过密（命中 {style_hits} 处）"))
    elif style_hits >= 2:
        issues.append(_issue("ai_cadence", "warning", f"存在 {style_hits} 处高频模板化连接词，建议改成事实衔接"))

    normalized_paragraphs = [_plain(p) for p in paragraphs if len(_plain(p)) >= 36]
    if len(normalized_paragraphs) >= 3 and len(set(normalized_paragraphs)) < len(normalized_paragraphs):
        issues.append(_issue("duplicate_paragraph", "error", "正文存在重复段落"))
    if paragraphs and all(len(_plain(p)) < 55 for p in paragraphs):
        issues.append(_issue("fragmented", "warning", "正文段落过碎，缺少连续解释"))

    # 只在源文和成稿都足够长时计算整体相似度；短新闻必然共享实体和事实词，
    # 不应被机械判成抄写。
    for item in material:
        source = _plain(item.get("content") or item.get("summary_short"))
        if len(source) < 700 or len(plain) < 700:
            continue
        ratio = SequenceMatcher(None, plain, source, autojunk=False).ratio()
        if ratio >= 0.76:
            issues.append(_issue(
                "source_rewrite", "error",
                f"正文与单一来源整体相似度过高（约 {ratio:.0%}），缺少重新组织和读者增值",
            ))
            break

    if digest and title and SequenceMatcher(None, _plain(digest), _plain(title), autojunk=False).ratio() >= 0.88:
        issues.append(_issue("intro_title_copy", "warning", "内容简介几乎只是标题重复，缺少页面摘要信息"))

    keywords = _terms(data.get("page_keywords"))
    tags = _terms(data.get("tags"))
    if len(keywords) != 3:
        issues.append(_issue("keyword_shape", "warning", "页面关键字应恰好是 3 个真实搜索短语（品牌词、品牌加版本词、问题型长尾），而不是散乱词串"))
    if len(keywords) != len(set(keywords)):
        issues.append(_issue("keyword_duplicate", "error", "页面关键字存在重复"))
    if not 2 <= len(tags) <= 5:
        issues.append(_issue("tag_shape", "warning", "栏目标签应控制在 2~5 个"))

    # 单源文章不能伪装成多源调查；这条只要求写法诚实，不要求拒绝生成。
    if brief and brief.get("article_type") == "single_source_update" and len(material) == 1:
        if re.search(r"多家|多方|业内|行业普遍|各方", plain):
            issues.append(_issue("single_source_overclaim", "error", "单一来源素材却使用了多方/行业普遍等无依据表述"))

    issues.sort(key=lambda item: 0 if item["severity"] == "error" else 1)
    return issues


def blocking_quality_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in issues if item.get("severity") == "error"]


def quality_feedback(issues: list[dict[str, str]]) -> str:
    if not issues:
        return ""
    lines = [f"- {item['message']}" for item in issues]
    return (
        "上一版草稿未通过编辑部质量复核。请只针对以下问题重写，保留已经正确的事实，"
        "不要通过补充外部知识来掩盖问题：\n" + "\n".join(lines)
    )
