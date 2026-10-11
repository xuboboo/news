"""网站资讯文章生成：独立于公众号草稿的 SEO 友好编辑稿。"""
import json
import re
import time
from difflib import SequenceMatcher

import httpx
from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session

from app.ai import llm_client
from app.core.config import get_settings
from app.models.article_draft import ArticleDraft
from app.models.setting import SystemSetting
from app.services import config_service
from app.services.article_draft_rules import (
    SITE_COVER_GUIDANCE_LIMIT,
    SITE_DIGEST_LIMIT,
    SITE_KEYWORDS_LIMIT,
    SITE_TAGS_LIMIT,
    SITE_TITLE_LIMIT,
    source_refs_from_material,
)
from app.services.article_gen_service import (
    _collect_material,
    _first_paragraph,
    _strip_generated_footer_sections,
    _strip_stray_source_markers,
)
from app.services.config_service import effective_llm
from app.services.site_article_quality import (
    blocking_quality_issues,
    inspect_site_article,
    quality_feedback,
)
from app.services.usage_service import record_usage
from app.utils.text import truncate
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

# 网站稿是研究 + 写作两阶段，思考型模型非流式响应可能数分钟无数据。
# 不能沿用公众号的 180s；仅延长读取等待，连接/发送仍快速失败。
# JSON 修复仍最多一次、质量重写最多一次，网络超时不自动重试。
SITE_RESEARCH_TIMEOUT_SECONDS = 300
SITE_WRITING_TIMEOUT_SECONDS = 600
SITE_RESEARCH_MAX_TOKENS = 8192
SITE_WRITING_MAX_TOKENS = 12288
# GLM 等思考型模型的思考 token 计入 max_tokens：实测思考会吃满整个输出预算，
# 导致 JSON 在正文前被截断（parse_fail）。本管线的结构化提示词已内置核查与
# 自检流程，不依赖自由推理；两阶段均显式关闭思考（探针验证供应商支持）。
SITE_THINKING_MODE = "disabled"

SITE_RESEARCH_SYSTEM_PROMPT = (
    "你是新闻编辑部的事实核查编辑，不是文案作者。"
    "你的任务是把给定报道整理成可审计的编辑简报，不写成稿，不补充外部知识。\n"
    "硬规则：\n"
    "1. 每条事实必须能追溯到报道编号；报道标题和事件聚类摘要都不能替代正文证据。\n"
    "2. 明确区分已确认事实、来源声称、待确认信息和不同报道之间的冲突；不能替编辑消除不确定性。\n"
    "3. 素材只有一篇时，类型必须是 single_source_update；不得伪装成独家、多方调查或行业共识。\n"
    "4. 找出读者实际会搜索的问题，以及这些素材能回答的范围；素材回答不了的问题放入 must_not_claim。\n"
    "5. 设计一个来自素材之间对照、变化、限制条件或读者影响的编辑角度；不能把‘换一种说法’当成原创。\n"
    "检索增值：\n"
    "6. 列出这篇稿件必须出现的核心实体（产品、版本号、厂商、平台、文件格式、标准名），"
    "以及读者可能使用的同义写法，供写作阶段自然覆盖；不要为覆盖而堆词。\n"
    "7. 标出素材里最具引用价值的硬信息：版本号、具体日期、设备名或路径、受影响范围、限制条件、修复项数量。"
    "这类内容既提升原创含量，也最容易被搜索摘要和生成式搜索引用。\n"
    "8. 指出素材之间的互补与冲突：A 报道给了变化、B 报道给了限制条件时，"
    "要在 source_differences 里写清谁补了什么，写作阶段据此做对照，而不是把两篇合并成一篇复述。\n"
    "严格只输出 JSON 对象。"
)

SITE_WRITER_SYSTEM_PROMPT = (
    "你是科技媒体的资深编辑和记者，负责把经过事实核查的编辑简报写成可发布的网站资讯稿。"
    "文章首先服务于直接访问网站的读者，其次才服务于搜索引擎。\n"
    "一、事实边界（最高优先级）\n"
    "1. 只能使用报道正文和编辑简报中有来源编号的事实；不得调用训练记忆补背景。"
    "不得虚构测试、体验、专家观点、用户反馈、价格、日期、数据或后续影响。\n"
    "2. 来源编号只用于你的内部核对，正文不要出现编号、链接或‘据多家媒体’等没有证据的概括。"
    "官方说法、媒体转述和推测必须用准确的归属措辞，三级归属分别是："
    "‘官方公布/说明’（厂商、社区或项目自己的公告）、‘某媒体报道/转述’（媒体自己的说法）、"
    "‘目前未知/尚无公开信息’（素材确实没写）。不能把第三级写成第一级。\n"
    "3. 不逐句翻译或拼接单一报道。要把不同报道中的变化、限制和受影响人群重新组织成读者能用的信息；"
    "单一来源时诚实写成一篇信息完整的单源更新稿。\n"
    "4. 不为了显得原创而编造第一手体验。真正的原创只能来自素材之外的实测、采访、数据或分析；"
    "本任务没有这些材料时只做有根据的编辑增值：把限制条件讲清、把适用与不适用分开、把读者要做的判断列出来。\n"
    "二、反合理化清单（这些理由一律不成立，不要用它说服自己放宽事实边界）\n"
    "- ‘这是行业常识，不写反而看不懂’：常识不写在素材里就不写，改写成本文能核对的表述或直接省略。\n"
    "- ‘读者肯定想知道价格或发布日期’：素材没有就写‘目前未知’，不要给一个看起来合理的数值或时间。\n"
    "- ‘两篇报道都提到，所以是多方确认’：同源转载不算多方，先看编辑简报的 source_differences 再下判断。\n"
    "- ‘不加一句总结会显得不完整’：不完整好过不准确，正文可以停在最后一个有来源的事实上。\n"
    "三、语言与篇章（这一节决定文章像不像人写的，必须逐条执行）\n"
    "5. 第一段直接回答发生了什么、涉及谁/什么、对读者有什么具体影响；不要从‘据某媒体消息’或空泛背景开始。\n"
    "6. 每句话必须携带新的事实、具体对象，或对读者明确的取舍判断。只能靠连接词承上启下的句子，删掉。\n"
    "7. 小标题以具体信息的陈述式表达为主（如‘9.2.73 的五项新功能与入口’），"
    "全文问句式小标题最多 1 个，禁止自问自答腔——设问后立刻接‘答案是/就是’。"
    "禁止使用‘事件概述、背景介绍、影响分析、总结、相关信息’等空标题。\n"
    "8. 句式节奏：句长要有长有短，每段至少有一句短句（不超过 12 字）和一句长句（超过 40 字）；"
    "段与段、节与节之间不要用同一套句式开头。\n"
    "9. 禁令（命中即改写，不是建议）："
    "① 成簇使用‘此外、与此同时、值得注意的是、这意味着、整体来看、不难发现、综上所述、换言之、换句话说’；"
    "② ‘不仅……而且……’‘不是……而是……’这类否定式对仗；"
    "③ 把小标题或要点强行凑成三条的三段式；"
    "④ ‘随着……的发展’‘在……背景下’式开头；"
    "⑤ 破折号（全文最多一次，能改写就改写）；"
    "⑥ 一句话里出现三个以上‘的’，或连续使用‘进行+动词’‘被+动词’；"
    "⑦ 一句话里出现两个以上分号的长句，拆成短句；"
    "⑧ 把判断推给读者的修辞（‘读者可以自行判断’‘尚无定论’）——有结论就直说，"
    "事实未知就写‘目前未知’，不要用推责句式装作客观。\n"
    "10. 不写‘本文将带你了解’‘接下来我们’‘总的来说’‘由此可见’等模板化文章口吻；"
    "不写‘标志着……新时代’‘具有重要意义’‘为……奠定基础’这类拔高句；"
    "结尾落在最后一个具体事实上，不要为了收尾再加一段抽象总结。\n"
    "11. 不要为了去 AI 味故意加入口语、比喻、第一人称或情绪，也不要为了显得亲切而写祈使句。"
    "去 AI 味靠具体和节奏，不靠口癖。\n"
    "12. 只有素材确实支持时才写步骤、FAQ、购买建议、未来预测或行业影响；写不出来就删掉该部分，不要留空壳小节。\n"
    "四、搜索可理解性（SEO）。这一节同时服务传统搜索摘要与生成式搜索/AI 问答的引用："
    "可引用的具体数字、明确的归属、分层的限制条件，是有效做法；关键词堆砌不是。\n"
    "13. 主标题 22~34 字，结构是‘实体或事件 + 具体变化 + 关键限定词（版本/平台/范围）’；"
    "包含读者真正会搜索的实体、产品/版本或事件，不堆同义关键词，不制造悬念；标题与正文必须一致。\n"
    "14. content_intro 是当前页面的独立摘要：用 60~120 字说明具体变化、受影响对象和读者能拿到的判断依据；"
    "不复读标题、不写关键词串、不写‘本文介绍’，也不要整段摘抄正文第一段。\n"
    "15. page_keywords 恰好给 3 个自然搜索短语，按读者在搜索框里的真实写法给："
    "一个品牌词、一个品牌加版本词、一个问题型长尾（如‘XX 怎么设置’‘XX 支持哪些设备’）。"
    "它不是关键词堆砌，也不是把整篇标题拆开，不要把标题原句再抄一遍，不多不少正好 3 个。\n"
    "16. tags 只给 2~5 个可复用的主题标签，用产品线或技术领域命名；"
    "不要给‘软件更新’‘产品发布’这类体裁词，也不要给一次性事件名，避免站内标签爆炸。\n"
    "17. 正文每个小节的第一句先用一句陈述直接给出该小节的核心信息，再展开细节；"
    "关键数字、版本、平台、路径和限制条件必须以完整形态出现一次，便于被搜索摘要和 AI 回答直接引用。\n"
    "18. 关键数据成组出现时（版本号、日期区间、安装量、评分、价格等），"
    "用 Markdown 表格或列表独立呈现，不要把成组数据埋进段落长句里；表格内容必须逐项来自素材。\n"
    "19. 信息增量自查：动笔前检查素材里有没有可以整合成模块的内容——新旧功能对照、版本时间线、"
    "参与或使用路径、读者常见疑问；有就用表格或小节呈现，这是和上游报道拉开差距的关键；素材没有的项不编造、不硬凑。\n"
    "20. 正文使用 Markdown 的 ## 小标题，不输出一级标题、HTML、外链、图片 URL、"
    "也不输出参考来源小节、来源编号或‘参考来源：’这类尾注；正文从头到尾不出现任何来源信息。\n"
    "六、输出前自查（逐项过一遍，发现问题就改，不要输出自查过程）\n"
    "21. 标题、小标题、简介三处是否各自独立、都可读？简介是否只是标题复述？\n"
    "22. 每个关键数字和实体是否都能回到某条 R 报道？有没有把来源声称写成已确认事实？\n"
    "23. 通读一遍：有没有重复段落、空泛段尾、模板连接词成簇、三段式凑数、拔高句？\n"
    "24. 有没有单源改写痕迹——如果去掉来源名字，这篇文章是否只是原文的同义复述？"
    "如果是，补上限制条件、适用范围或受影响人群的对照。\n"
    "25. 正文是否出现了‘参考来源’‘R1’‘据多家媒体’等不该外泄的内容？出现就删。"
)

# 兼容现有调用方和测试：网站写作系统提示词的公开名称仍保留。
SITE_SYSTEM_PROMPT = SITE_WRITER_SYSTEM_PROMPT


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_longtail(value: object) -> list[dict[str, str]]:
    """清洗模型输出的长尾承接建议：只保留 keyword/intent/form/title 四项，最多 8 条。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        keyword = _clean(item.get("keyword"))
        title = _clean(item.get("title"))
        if not keyword or not title:
            continue
        intent = _clean(item.get("intent"))
        form = _clean(item.get("form"))
        out.append({
            "keyword": truncate(keyword, 40),
            "intent": intent if intent in ("高", "中", "低") else "中",
            "form": truncate(form, 10),
            "title": truncate(title, 60),
        })
    return out


def _list_text(value: object, limit: int) -> str:
    if isinstance(value, list):
        values = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    else:
        values = [part.strip() for part in re.split(r"[,，、\r\n]+", _clean(value)) if part.strip()]
    seen: list[str] = []
    for item in values:
        if item not in seen:
            seen.append(item)
    return truncate(",".join(seen), limit)


def _material_similarity(left: dict, right: dict) -> bool:
    left_title = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", _clean(left.get("title")).lower())
    right_title = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", _clean(right.get("title")).lower())
    if left_title and left_title == right_title:
        return True
    if left.get("source") != right.get("source"):
        return False
    left_body = re.sub(r"\s+", "", _clean(left.get("content")))[:1800]
    right_body = re.sub(r"\s+", "", _clean(right.get("content")))[:1800]
    return bool(left_body and right_body and len(left_body) >= 500 and
                SequenceMatcher(None, left_body, right_body, autojunk=False).ratio() >= 0.9)


def _select_site_material(material: list[dict]) -> list[dict]:
    """去掉同标题/同源近重复报道，避免模型把转载堆叠误写成多源事实。"""
    selected: list[dict] = []
    for item in material:
        duplicate = next((index for index, old in enumerate(selected)
                          if _material_similarity(item, old)), None)
        if duplicate is None:
            selected.append(item)
        elif len(_clean(item.get("content"))) > len(_clean(selected[duplicate].get("content"))):
            selected[duplicate] = item
    return selected


def _infer_article_type(title: str, material: list[dict]) -> str:
    text = title + " " + " ".join(_clean(item.get("title")) for item in material[:3])
    if len(material) == 1:
        return "single_source_update"
    if re.search(r"预警|漏洞|攻击|安全更新|证书|截止|禁用|停服|下线|召回", text):
        return "system_alert"
    if re.search(r"发布|上线|开源|推出|亮相|官宣", text) and re.search(r"产品|工具|模型|应用|版本|服务", text):
        return "product_launch"
    if re.search(r"更新|升级|版本|补丁|修复|新增支持", text):
        return "release_update"
    if re.search(r"事故|故障|中断|泄露|回应|调查|进展", text):
        return "incident"
    return "general_event"


def _material_text(material: list[dict]) -> str:
    blocks: list[str] = []
    for index, item in enumerate(material, 1):
        body = _clean(item.get("content")) or _clean(item.get("summary_short")) or "（正文缺失，仅标题可用）"
        published = _clean(item.get("publish_time")) or "未知"
        blocks.append(
            f"【R{index}】来源：{_clean(item.get('source'))}；发布时间：{published}\n"
            f"标题：{_clean(item.get('title'))}\n正文：\n{body}"
        )
    return "\n\n".join(blocks)


def _fallback_brief(row: ArticleDraft, material: list[dict]) -> dict:
    article_type = _infer_article_type(row.source_title, material)
    return {
        "article_type": article_type,
        "primary_search_intent": row.source_title,
        "reader_questions": ["发生了什么？", "哪些读者会受到影响？"],
        "news_angle": "从素材中明确的变化、限制条件和读者影响重新组织信息。",
        "one_sentence_thesis": row.source_title,
        "facts": [
            {"id": f"F{index}", "claim": _clean(item.get("title")), "source_ids": [f"R{index}"], "certainty": "reported"}
            for index, item in enumerate(material, 1)
        ],
        "entities": [],
        "quotable_details": [],
        "source_differences": [],
        "outline": [],
        "must_not_claim": ["素材没有明确出现的背景、数据、体验、预测和评价"],
    }


_ARTICLE_TYPES = {
    "single_source_update", "release_update", "product_launch",
    "system_alert", "incident", "general_event",
}


def _valid_editorial_brief(value: object, material: list[dict]) -> bool:
    if not isinstance(value, dict) or value.get("article_type") not in _ARTICLE_TYPES:
        return False
    facts = value.get("facts")
    if not isinstance(facts, list) or not facts:
        return False
    valid_sources = {f"R{index}" for index in range(1, len(material) + 1)}
    for fact in facts:
        if not isinstance(fact, dict) or not _clean(fact.get("claim")):
            return False
        source_ids = fact.get("source_ids")
        if not isinstance(source_ids, list) or not set(source_ids) <= valid_sources:
            return False
    outline = value.get("outline")
    return isinstance(outline, list) and all(
        isinstance(item, dict) and _clean(item.get("heading")) for item in outline
    )


def _editorial_brief_prompt(row: ArticleDraft, material: list[dict], event_summary: str) -> str:
    event_block = (
        "事件聚类摘要（仅作线索，可能有误，不得当作事实；必须回到报道正文核对）：\n"
        + event_summary + "\n\n"
        if event_summary else ""
    )
    return (
        "请为下面的科技资讯选题建立编辑简报。先判断这篇稿件应该回答什么问题，再建立事实账本和文章结构；不要写成稿。\n\n"
        f"选题：{row.source_title}\n{event_block}"
        f"报道材料（R 编号是唯一引用依据）：\n{_material_text(material)}\n\n"
        "简报是供写作使用的短提纲，不是长篇分析。最多 12 条不重复事实，每条只写一项具体事实；"
        "reader_questions 最多 4 条、outline 最多 6 节、source_differences 和 must_not_claim 各最多 4 条，"
        "entities 和 quotable_details 各最多 8 条。"
        "保留关键实体、版本、日期、限制和对应来源；只输出这些字段，不输出思考、自评、重复背景或成稿。\n"
        "entities 写读者会搜的实体及其同义写法；quotable_details 写可直接被搜索摘要引用的硬信息"
        "（版本号、日期、路径、设备名、数量、限制条件），每条注明来源。\n"
        "输出 JSON，字段必须为："
        '{"article_type":"single_source_update|release_update|product_launch|system_alert|incident|general_event",'
        '"primary_search_intent":"读者会搜索的具体问题",'
        '"reader_questions":["问题1","问题2"],'
        '"news_angle":"素材支持的编辑增值角度",'
        '"one_sentence_thesis":"一句话主旨",'
        '"facts":[{"id":"F1","claim":"可核对事实","source_ids":["R1"],"certainty":"confirmed|reported|uncertain"}],'
        '"entities":[{"name":"实体名","aliases":["同义写法"],"source_ids":["R1"]}],'
        '"quotable_details":[{"detail":"版本号/日期/路径/限制等硬信息","source_ids":["R1"]}],'
        '"source_differences":["报道之间的差异或冲突；没有则为空数组"],'
        '"outline":[{"heading":"具体小标题","purpose":"本节给读者的答案","fact_ids":["F1"]}],'
        '"must_not_claim":["素材不足不能写的内容"]}'
    )


def _ground_block(brief: dict) -> str:
    """把简报里的实体与硬信息压成一行写作锚点，避免正文丢掉可被搜索引用的具体内容。"""
    entities: list[str] = []
    for item in brief.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        aliases = [a for a in (item.get("aliases") or []) if isinstance(a, str) and a.strip()]
        if name:
            entities.append(name + (f"（{('、'.join(aliases[:3]))}）" if aliases else ""))
    details: list[str] = []
    for item in brief.get("quotable_details") or []:
        if not isinstance(item, dict):
            continue
        detail = _clean(item.get("detail"))
        if detail:
            details.append(detail)
    lines: list[str] = []
    if entities:
        lines.append("必须在正文中自然出现的实体：" + "；".join(entities[:8]))
    if details:
        lines.append("可直接被搜索摘要与 AI 回答引用的硬信息：" + "；".join(details[:8]))
    if not lines:
        return ""
    return (
        "- 素材锚点（以下是简报列出的检索与引用线索，只能按 R 编号核对后使用，不得据此补充新事实）："
        + "\n  ".join(["", *lines])
        + "\n"
    )


def _site_prompt(row: ArticleDraft, material: list[dict], event_summary: str, brief: dict | None = None) -> str:
    brief = brief or _fallback_brief(row, material)
    article_type = str(brief.get("article_type") or _infer_article_type(row.source_title, material))
    type_rules = {
        "release_update": "重点回答更新了什么、哪些用户会受到影响、兼容性/限制是什么；版本号和修复项按事实取舍。",
        "product_launch": "重点回答发布了什么、核心能力是什么、面向谁、价格/可用性/限制是什么；没有的字段不要补。",
        "system_alert": "重点回答谁受影响、截止时间或触发条件、会发生什么、官方建议怎么做；把未知范围写清楚。",
        "incident": "按已确认时间线、影响范围、处置进展和仍未知事项组织；不要把传闻写成结论。",
        "single_source_update": "写成信息完整的单源更新稿；可以解释读者影响，但不能使用多方、行业普遍、独家等无依据表述。",
        "general_event": "围绕发生了什么、证据有哪些、对相关用户意味着什么组织；只在素材支持时写后续观察。",
    }.get(article_type, "围绕事实变化和读者影响组织，素材不足就缩短。")
    tier_rule = (
        "深度版可写 3~6 个小节，必须有跨报道对照或限制条件；没有足够事实时宁可短。"
        if row.tier == "depth" else
        "快讯版写 2~4 个小节，优先完整回答读者问题，不为长度补背景。"
    )
    event_block = (
        "事件聚类摘要（不具备事实优先级，仅供发现线索）：\n" + event_summary + "\n\n"
        if event_summary else ""
    )
    ground_block = _ground_block(brief)
    brief_json = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    return (
        "根据编辑简报和原始报道，写一篇供人工审核的网站科技资讯稿。先在脑中核对事实，最终只输出 JSON。\n\n"
        f"选题：{row.source_title}\n文章类型：{article_type}\n{event_block}"
        f"编辑简报：\n{brief_json}\n\n报道材料：\n{_material_text(material)}\n\n"
        "写作要求：\n"
        f"- {type_rules}\n- {tier_rule}\n"
        "- 导语第一段 60~120 字，直接交代事件和读者影响；不要从媒体署名、泛泛背景或‘近年来’开始。\n"
        "- 段落节奏：段落之间长度要有差别，避免连续三段都是相近字数；允许 40 字左右的短段，"
        "但正文整体不要只由短段堆成。数字、版本号、路径、设备名和限制条件的原样枚举不算空话，可以保留。\n"
        "- 每个小节的第一句先直接回答该小节标题提出的问题，再展开细节；不要让读者读完整节才知道答案。\n"
        "- 小标题使用具体事实或读者问题，避免模板标题；正文不要重复主标题；小标题数量决定信息分期，不要为了凑结构强行加节。\n"
        "- 关键事实、数字、版本、时间、产品名和限制条件必须能回到 R 编号；冲突信息要保留归属或明确未知。\n"
        f"{ground_block}"
        "- 不逐句改写任何来源，不把几篇相同转载拼成原创；优先做事实对照、变化解释和受影响人群说明。\n"
        "- 不写外部背景、教程步骤、FAQ、用户评价、专家判断、未来预测或第一手体验，除非报道明确支持。\n"
        "- 不使用本文将、带你了解、值得注意的是、与此同时、总的来说、整体来看等成簇套话；"
        "也不要为了去 AI 味故意加入口语、比喻、第一人称，更不要用‘不是……而是……’这类对仗句式。\n"
        f"- title 20~30 字（上限 {SITE_TITLE_LIMIT} 字），结构‘主体+事件+关键信息’；具体、可检索、不过度承诺；"
        "不堆‘最新/大全/免费/必看’这类词；给出 3 个不同取向但都事实准确的备选标题：分别偏‘发生了什么事’、偏‘读者会搜的具体问题’、偏‘版本与适用范围’。\n"
        f"- page_keywords 恰好给出 3 个自然搜索短语（品牌词、品牌加版本词、问题型长尾各一），总长度不超过 {SITE_KEYWORDS_LIMIT} 字；"
        "不要只是把标题切开或抄一遍，至少包含一个品牌加版本词和一个问题型长尾。\n"
        f"- tags 给出 2~5 个可复用主题标签，用产品线或技术领域命名，总长度不超过 {SITE_TAGS_LIMIT} 字；"
        "不要用‘软件更新’‘产品发布’这类体裁词或一次性事件名。\n"
        f"- content_intro 是当前页面的独立摘要，60~100 字（上限 {SITE_DIGEST_LIMIT} 字），核心词自然出现在前半句；"
        "写明具体变化、受影响对象和读者能得到什么；不要复读标题、不要关键词堆砌、不要写‘本文介绍’，"
        "也不要直接摘抄正文第一段。\n"
        f"- cover_guidance 只写与事实相关的 1:1 标题图构图建议，不超过 {SITE_COVER_GUIDANCE_LIMIT} 字。\n"
        "- 不输出一级标题、HTML、外链、图片 URL、参考来源小节或写作过程；正文从头到尾不出现任何来源信息。\n"
        "- 正文严禁出现 R 编号、‘参考来源：’‘据多家媒体’这类尾注或来源清单，出现即视为不合格。\n"
        "- longtail_suggestions：给出 5~8 个从本文素材拆出的长尾承接选题，供后续单独成篇；"
        "每条含 keyword（带‘怎么/在哪/能不能/为什么’等疑问词的具体长尾词，不要‘QQ鸿蒙版’这类大词）、"
        "intent（高/中/低，按用户意图明确度）、form（教程/问答/对比/资讯）、"
        "title（建议页面标题，15~30 字，不写‘最全攻略/终极指南’）。只写素材能支撑的方向，不编造功能。\n"
        f"- 正文（不含小标题）不少于 {320 if row.tier == 'brief' else 520} 字，"
        "但不要为了凑字数重复已写过的信息；字数是完整回答读者问题的结果，不是目标。\n"
        "- software_id 固定为 0，软件关联由编辑人工选择。\n"
        '严格输出一个 JSON 对象：{"title":"网站文章标题","alt_titles":["备选1","备选2","备选3"],'
        '"page_keywords":"关键词1,关键词2","tags":"标签1,标签2","content_intro":"内容简介",'
        '"cover_guidance":"标题图片构图建议","software_id":0,"content_md":"Markdown 正文",'
        '"longtail_suggestions":[{"keyword":"长尾词","intent":"高","form":"教程","title":"建议页面标题"}]}'
    )


def _candidate_rank(data: dict, issues: list[dict[str, str]], tier: str) -> tuple[int, int, int]:
    """写作候选择优排序键：阻断最少 → 警告最少 → 标题落在 22~34 字且信息量达标。"""
    errors = sum(1 for item in issues if item.get("severity") == "error")
    warnings = sum(1 for item in issues if item.get("severity") == "warning")
    title_len = len(_clean(data.get("title")))
    title_penalty = 0 if 22 <= title_len <= 34 else 1
    body_len = len(re.sub(r"\s+", "", str(data.get("content_md") or "")))
    length_penalty = 0 if body_len >= (520 if tier == "depth" else 360) else 1
    return (errors, warnings, title_penalty + length_penalty)


def _rewrite_prompt(original: dict, issues: list[dict[str, str]]) -> str:
    feedback = quality_feedback(issues)
    return (
        "\n\n编辑部复核意见：\n" + feedback + "\n"
        "请重新输出完整 JSON，不要只给修改片段。保留已核实事实，删除无法满足来源边界的句子，"
        "不要新增外部事实。上一次 JSON 仅供定位问题：\n" + json.dumps(original, ensure_ascii=False)
    )


def _fail(db: Session, row: ArticleDraft, error: str, duration_ms: int, generation: int) -> None:
    changed = (
        db.query(ArticleDraft)
        .filter(ArticleDraft.id == row.id, ArticleDraft.channel == "site",
                ArticleDraft.status == "generating", ArticleDraft.gen_count == generation)
        .update({ArticleDraft.status: "failed", ArticleDraft.error: truncate(error, 500),
                 ArticleDraft.duration_ms: duration_ms, ArticleDraft.revision: ArticleDraft.revision + 1},
                synchronize_session=False)
    )
    if changed:
        db.commit()
    else:
        db.rollback()


def _merge_usage(total: dict, usage: dict | None) -> None:
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int((usage or {}).get(key) or 0)


def _request_json(
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    prompt: str,
    system: str,
    temperature: float,
    record,
    enabled,
    *,
    stage: str = "写作",
    read_timeout_seconds: float = SITE_WRITING_TIMEOUT_SECONDS,
    max_tokens: int = SITE_WRITING_MAX_TOKENS,
) -> tuple[dict | None, dict, str | None]:
    """单阶段最多两次 JSON 尝试；网络超时立即停止，避免重复计费。

    返回该阶段所有尝试的已知用量，而不是只返回最后一次。流水延迟按单次
    请求记录，不能用整篇生成的累计耗时冒充第二阶段的模型响应时间。
    """
    feedback = ""
    total_usage: dict = {}
    request_timeout = httpx.Timeout(
        connect=20, read=read_timeout_seconds, write=30, pool=20,
    )
    for _attempt in range(2):
        usage: dict = {}
        if not enabled():
            return None, total_usage, "disabled"
        call_start = time.monotonic()

        def record_attempt(status: str, error: str | None) -> None:
            _merge_usage(total_usage, usage)
            record(status, error, latency_ms=int((time.monotonic() - call_start) * 1000), **usage)

        try:
            raw, usage = llm_client.chat(
                base_url, api_key, model, protocol, prompt + feedback,
                system=system, temperature=temperature, json_mode=True,
                timeout=request_timeout, max_tokens=max_tokens,
                thinking=SITE_THINKING_MODE,
            )
            data = llm_client.extract_json_object(raw)
            if not isinstance(data, dict):
                raise ValueError("AI 输出不是 JSON 对象")
            record_attempt("success", None)
            return data, total_usage, None
        except llm_client.LLMTimeoutError as exc:
            if exc.phase == "read":
                error = (
                    f"等待模型响应超时（读取等待上限 {read_timeout_seconds:g} 秒）；"
                    "本次结果未收到，供应商用量无法确认。为避免重复计费，未自动重试，请稍后手动重新生成。"
                )
            else:
                phase, limit = {
                    "connect": ("连接", request_timeout.connect),
                    "write": ("发送请求", request_timeout.write),
                    "pool": ("获取连接", request_timeout.pool),
                }.get(exc.phase, ("请求", read_timeout_seconds))
                error = f"模型服务{phase}超时（等待上限 {limit:g} 秒），未自动重试，请稍后手动重新生成。"
            record_attempt("api_fail", f"{stage}阶段：{error}")
            return None, total_usage, error
        except llm_client.LLMAPIError as exc:
            record_attempt("api_fail", f"{stage}阶段：{exc}")
            return None, total_usage, str(exc)
        except (ValueError, llm_client.LLMFormatError) as exc:
            usage = getattr(exc, "usage", None) or usage
            record_attempt("parse_fail", f"{stage}阶段：{exc}")
            feedback = llm_client.JSON_RETRY_HINT.format(error=exc)
    return None, total_usage, "parse failed"


def generate_site_article_for_draft(
    db: Session, draft_id: int, trigger: str = "manual", generation: int | None = None,
) -> tuple[bool, str]:
    """生成网站稿；与公众号生成共享素材、限额、用量和代次围栏，但提示词独立。"""
    if db.bind is None or db.bind.dialect.name == "postgresql":
        acquired = db.execute(
            sql_text("SELECT pg_try_advisory_xact_lock(hashtext('article_draft:' || :i))"),
            {"i": draft_id},
        ).scalar()
        if not acquired:
            return False, "another generation in flight"
    row = db.get(ArticleDraft, draft_id, populate_existing=True)
    if row is None or row.channel != "site":
        return False, "draft not found"
    if row.status != "generating" or (generation is not None and row.gen_count != generation):
        return False, "generation superseded"
    expected_generation = row.gen_count
    start = time.monotonic()
    duration_ms = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731

    def enabled() -> bool:
        value = db.execute(select(SystemSetting.value).where(SystemSetting.key == "article_gen_enabled")).scalar_one_or_none()
        if value is None or value == "":
            return bool(get_settings().article_gen_enabled)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    if not enabled():
        _fail(db, row, "文章生成已在运维面板关闭（article_gen_enabled=false）", duration_ms(), expected_generation)
        return False, "article generation disabled"

    base_url, api_key, base_model, protocol = effective_llm(db)
    override = str(config_service.get_effective(db, "article_gen_model") or "").strip()
    model = override or base_model

    def record(status: str, error: str | None, *, latency_ms: int | None = None, **usage) -> None:
        record_usage(task_type="site_article", model=model, status=status, error=error,
                     latency_ms=latency_ms if latency_ms is not None else duration_ms(), trigger=trigger,
                     event_id=row.source_id if row.source_type == "event" else None,
                     article_id=row.source_id if row.source_type == "article" else None, **usage)

    if len(model) > 64:
        error = "模型标识超过用量流水支持的 64 字符上限，未调用模型"
        _fail(db, row, error, duration_ms(), expected_generation)
        return False, error
    if not (base_url and api_key and model):
        error = "未配置 LLM（llm_base_url / llm_api_key / llm_model），请在运维面板配置"
        _fail(db, row, error, duration_ms(), expected_generation)
        return False, "llm not configured"
    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        record("api_fail", str(exc))
        _fail(db, row, f"LLM 地址被 SSRF 校验拦截：{exc}", duration_ms(), expected_generation)
        return False, str(exc)

    material, event_summary = _collect_material(db, row)
    material = _select_site_material(material)
    if not material:
        _fail(db, row, "素材为空：源内容可能已被清理，无法生成", duration_ms(), expected_generation)
        return False, "no material"

    total_usage: dict = {}
    brief, usage, brief_error = _request_json(
        base_url, api_key, model, protocol,
        _editorial_brief_prompt(row, material, event_summary),
        SITE_RESEARCH_SYSTEM_PROMPT, 0.15, record, enabled,
        stage="研究", read_timeout_seconds=SITE_RESEARCH_TIMEOUT_SECONDS,
        max_tokens=SITE_RESEARCH_MAX_TOKENS,
    )
    _merge_usage(total_usage, usage)
    if brief_error == "disabled":
        _fail(db, row, "文章生成已在运维面板关闭（article_gen_enabled=false）", duration_ms(), expected_generation)
        return False, "article generation disabled"
    if brief is None:
        # 研究阶段格式失败时采用确定性的保守简报；API/网络失败仍然终止，
        # 不能在没有任何研究结果的情况下继续消耗写作阶段 token。
        if brief_error not in ("parse failed", None):
            _fail(db, row, f"编辑简报阶段 LLM 调用失败：{brief_error}", duration_ms(), expected_generation)
            return False, brief_error
        brief = _fallback_brief(row, material)
    elif not _valid_editorial_brief(brief, material):
        brief = _fallback_brief(row, material)

    writer_prompt = _site_prompt(row, material, event_summary, brief)

    def _write_once(temperature: float, stage: str, feedback: str = ""):
        data, usage, writer_error = _request_json(
            base_url, api_key, model, protocol,
            writer_prompt + feedback, SITE_WRITER_SYSTEM_PROMPT, temperature, record, enabled,
            stage=stage, read_timeout_seconds=SITE_WRITING_TIMEOUT_SECONDS,
            max_tokens=SITE_WRITING_MAX_TOKENS,
        )
        _merge_usage(total_usage, usage)
        if writer_error == "disabled":
            return None, None, "disabled"
        if data is None:
            return None, None, writer_error or "writer failed"
        normalized = dict(data)
        normalized["title"] = _clean(data.get("title"))
        normalized["content_md"] = _clean(data.get("content_md"))
        normalized["content_intro"] = _clean(data.get("content_intro"))
        normalized["page_keywords"] = _list_text(data.get("page_keywords"), SITE_KEYWORDS_LIMIT)
        normalized["tags"] = _list_text(data.get("tags"), SITE_TAGS_LIMIT)
        issues = inspect_site_article(normalized, material, brief, row.tier)
        return normalized, issues, ""

    # 写作阶段：成本不设限、质量优先——每篇先产出两个独立候选，全部合格则自动择优
    # （成本换稳定性的确定性手段：单次采样的小节/标题波动会被择优抹平）。
    # 任一调用超时/接口失败仍立即终止，沿用“网络超时不自动重试”的既有契约。
    data: dict | None = None
    issues: list[dict[str, str]] = []
    ranked: list[tuple[tuple[int, int, int], dict, list[dict[str, str]]]] = []
    for temperature in (0.45, 0.6):
        data, issues, writer_error = _write_once(temperature, "写作")
        if writer_error == "disabled":
            _fail(db, row, "文章生成已在运维面板关闭（article_gen_enabled=false）", duration_ms(), expected_generation)
            return False, "article generation disabled"
        if writer_error:
            _fail(db, row, f"网站稿写作阶段失败：{writer_error}", duration_ms(), expected_generation)
            return False, writer_error
        ranked.append((_candidate_rank(data, issues, row.tier), data, issues))
        data = None
    ranked.sort(key=lambda item: item[0])
    if ranked and ranked[0][0][0] == 0:
        # 至少一个候选通过质量门：取排序最优者
        _, data, issues = ranked[0]
    if data is None:
        # 两个候选都未过质量门：取较优候选的问题清单定向重写一次（契约：重写最多一次）
        _, data, issues = ranked[0]
        data, issues, writer_error = _write_once(0.35, "质量重写", _rewrite_prompt(data, issues))
        if writer_error == "disabled":
            _fail(db, row, "文章生成已在运维面板关闭（article_gen_enabled=false）", duration_ms(), expected_generation)
            return False, "article generation disabled"
        if writer_error:
            _fail(db, row, f"网站稿写作阶段失败：{writer_error}", duration_ms(), expected_generation)
            return False, writer_error
        if blocking_quality_issues(issues):
            messages = "；".join(item["message"] for item in issues[:4])
            _fail(db, row, f"网站稿未通过编辑质量复核：{messages}", duration_ms(), expected_generation)
            return False, "quality gate failed"

    if data is None:
        _fail(db, row, "网站稿生成结果为空，请重新生成", duration_ms(), expected_generation)
        return False, "empty result"
    title = _clean(data.get("title"))
    content_md = _clean(data.get("content_md"))
    if not title or not content_md:
        _fail(db, row, "AI 输出缺少标题或正文，请重新生成", duration_ms(), expected_generation)
        return False, "missing fields"

    raw_alt_titles = data.get("alt_titles")
    if not isinstance(raw_alt_titles, list):
        raw_alt_titles = []
    alt_titles = [truncate(_clean(item), SITE_TITLE_LIMIT) for item in raw_alt_titles[:3]
                  if isinstance(item, str) and _clean(item)]
    source_refs = source_refs_from_material(material)
    # 应编辑要求（2026-10-11）：网站稿正文彻底不含参考来源小节/尾注；
    # 仍保留 source_refs 快照供审计与前端溯源，但不再写入正文。
    content_md = _strip_generated_footer_sections(_strip_stray_source_markers(content_md))
    site_keywords = _list_text(data.get("page_keywords"), SITE_KEYWORDS_LIMIT)
    site_tags = _list_text(data.get("tags"), SITE_TAGS_LIMIT)
    longtail = _normalize_longtail(data.get("longtail_suggestions"))
    cover_guidance = truncate(_clean(data.get("cover_guidance")), SITE_COVER_GUIDANCE_LIMIT)
    digest = truncate(_clean(data.get("content_intro")) or _first_paragraph(content_md), SITE_DIGEST_LIMIT)
    changed = (
        db.query(ArticleDraft)
        .filter(ArticleDraft.id == draft_id, ArticleDraft.channel == "site",
                ArticleDraft.status == "generating", ArticleDraft.gen_count == expected_generation)
        .update({ArticleDraft.title: truncate(title, SITE_TITLE_LIMIT), ArticleDraft.alt_titles: alt_titles,
                 ArticleDraft.seo_keywords: site_keywords, ArticleDraft.site_tags: site_tags,
                 ArticleDraft.longtail_suggestions: longtail,
                 ArticleDraft.cover_guidance: cover_guidance, ArticleDraft.software_id: 0,
                 ArticleDraft.digest: digest, ArticleDraft.content_md: content_md,
                 ArticleDraft.source_refs: source_refs, ArticleDraft.model: model,
                 ArticleDraft.prompt_tokens: int(total_usage.get("prompt_tokens") or 0),
                 ArticleDraft.completion_tokens: int(total_usage.get("completion_tokens") or 0),
                 ArticleDraft.status: "ready", ArticleDraft.error: "",
                 ArticleDraft.duration_ms: duration_ms(), ArticleDraft.revision: ArticleDraft.revision + 1},
                synchronize_session=False)
    )
    if not changed:
        db.rollback()
        return False, "generation superseded"
    db.commit()
    return True, ""
