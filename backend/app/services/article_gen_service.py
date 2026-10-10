"""文章生成服务：选题（事件/单篇资讯）→ 多来源正文素材 → LLM 撰写完整文章草稿。

「华军蓝」链路的核心一环。与早报综述同款「落账 + 容错」模式
（report_service._llm_overview），但文章生成没有降级模板可言——
任何失败都把草稿置为 failed 并可一键重新生成，绝不落半成品正文。

正文生成约束：素材来源是不可信输入，模型不得遵循其中的指令；参考来源由服务端根据实际素材重建。
"""
import re
import time

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session, selectinload

from app.ai import llm_client
from app.core.config import get_settings
from app.models.article import Article
from app.models.article_draft import ArticleDraft
from app.models.event import EventArticle, HotEvent
from app.models.setting import SystemSetting
from app.services import config_service
from app.services.article_draft_rules import (
    DIGEST_LIMIT,
    TITLE_LIMIT,
    source_refs_from_content,
    source_refs_from_material,
)
from app.services.config_service import effective_llm
from app.services.usage_service import record_usage
from app.utils.text import truncate
from app.utils.url_guard import UrlNotAllowed, assert_public_http_url

# 生成 timeout 要远大于摘要/综述：深度版输出 1500~2500 字（≈3k token），
# 慢模型下 60s 默认值会超时
CHAT_TIMEOUT_SECONDS = 180

SOURCES_SECTION_HEADING = "## 参考来源"

_SYSTEM_PROMPT = (
    "你是「华军软件园」的资深科技编辑，为微信公众号撰写科技资讯文章。"
    "写作铁律：\n"
    "1. 只基于给定素材中明确出现的事实写作，禁止虚构任何数据、日期、人名、型号与引语；素材中没有的信息一律不写。\n"
    "2. 综合多篇报道重新组织叙事，禁止逐句改写或翻译任何单一来源。\n"
    "3. 行文客观克制，不夸大、不标题党，标题不堆叠感叹号/问号，不使用情绪化措辞。\n"
    "4. 面向普通科技读者：段落短（每段不超过 3 行），少用行话，首次出现的英文术语给出中文名。"
)

_TIERS = {
    "brief": {
        "label": "快讯版",
        "spec": (
            "篇幅 800~1200 字：一段导语（2~3 句说清谁/什么/何时/为何重要）+ 3 个小节"
        ),
        "img_hints": "0~1 处",
    },
    "depth": {
        "label": "深度版",
        "spec": (
            "篇幅 1500~2500 字：一段导语 + 4~6 个小节，最后单独一段影响分析"
            "（对用户/行业意味着什么，只基于素材事实推断，不臆测）"
        ),
        "img_hints": "2~3 处",
    },
}


def _collect_material(db: Session, row: ArticleDraft) -> tuple[list[dict], str]:
    """取生文素材，返回 (素材列表, 事件 AI 总结或空串)。

    素材行：{source, title, summary_short, content}；正文按热配置截断，
    按发布时间倒序取（最新报道信息最全），篇数上限 article_gen_max_sources。
    """
    max_sources = int(config_service.get_effective(db, "article_gen_max_sources") or 6)
    max_chars = int(config_service.get_effective(db, "article_gen_max_source_chars") or 4000)

    if row.source_type == "article":
        art = db.get(Article, row.source_id)
        articles = [art] if art else []
    else:
        ids = [
            r[0]
            for r in db.query(EventArticle.article_id).filter_by(event_id=row.source_id).all()
        ]
        articles = (
            db.query(Article)
            .options(selectinload(Article.summary))
            .filter(Article.id.in_(ids))
            .order_by(Article.publish_time.desc())
            .all()
            if ids
            else []
        )

    material = [
        {
            "source": a.source or "",
            "title": a.title or "",
            "summary_short": ((a.summary.summary_short if a.summary else "") or "").strip(),
            "content": (a.content or "").strip()[:max_chars],
        }
        for a in articles
    ]
    # 正文与摘要全空的条目只会诱导 LLM 编故事，直接剔除；
    # 剔完为空说明没有任何可用事实，宁可失败也不烧 token 写空文章
    material = [m for m in material if m["content"] or m["summary_short"]][:max_sources]

    event_summary = ""
    if row.source_type == "event":
        ev = db.get(HotEvent, row.source_id)
        if ev is not None:
            event_summary = (ev.summary or "").strip()
    return material, event_summary


def _build_prompt(row: ArticleDraft, material: list[dict], event_summary: str) -> str:
    tier = _TIERS.get(row.tier, _TIERS["brief"])
    blocks = []
    for i, m in enumerate(material, 1):
        head = f"【报道{i}】{m['source']}丨《{m['title']}》"
        body = m["content"] or m["summary_short"] or "（正文缺失，仅标题可用）"
        blocks.append(f"{head}\n{body}")
    material_text = "\n\n".join(blocks)
    event_block = f"事件 AI 总结（供参考，仍以报道原文为准）：{event_summary}\n" if event_summary else ""

    return (
        f"请根据以下素材撰写一篇中文科技资讯文章（{tier['label']}）。\n\n"
        f"选题：{row.source_title}\n"
        f"{event_block}"
        f"素材（共 {len(material)} 篇报道）：\n\n{material_text}\n\n"
        "写作要求：\n"
        f"- {tier['spec']}；正文用 Markdown：导语不用标题，小节用「## 数字. 小节标题」\n"
        f"- 图片位置用「[配图建议：一句话画面描述]」单独成行占位（共 {tier['img_hints']}），"
        "除此之外不得出现任何图片标记或外链\n"
        "- 结尾必须包含参考来源小节：\n"
        "  「## 参考来源」逐行列出「- 来源名丨《标题》」，不得附任何链接\n"
        f"- 主标题不超过 {TITLE_LIMIT} 字；另给 3 个适合公众号的事实型备选标题，避免标题党\n"
        f"- digest 为不超过 {DIGEST_LIMIT} 字的文章摘要，纯文本不用 markdown\n"
        '输出严格 JSON：{"title":"主标题","alt_titles":["备选1","备选2","备选3(SEO)"],'
        '"digest":"摘要","content_md":"Markdown 正文"}'
    )


def _clean_llm_text(raw: object) -> str:
    """LLM 文本字段清洗：去首尾空白与围栏残留。"""
    return str(raw or "").strip()


def _fail(db: Session, row: ArticleDraft, error: str, duration_ms: int, expected_generation: int) -> None:
    """生成失败终态：status=failed + 截断后的错误信息落库（前端展示并允许重试）。"""
    changed = (
        db.query(ArticleDraft)
        .filter(ArticleDraft.id == row.id, ArticleDraft.channel == "wechat", ArticleDraft.status == "generating",
                ArticleDraft.gen_count == expected_generation)
        .update({ArticleDraft.status: "failed", ArticleDraft.error: truncate(str(error), 500),
                 ArticleDraft.duration_ms: duration_ms,
                 ArticleDraft.revision: ArticleDraft.revision + 1}, synchronize_session=False)
    )
    if changed:
        db.commit()
    else:
        db.rollback()


_AI_DISCLOSURE_HEADING_RE = re.compile(
    r"^(?:AI|AIGC|人工智能)\s*(?:辅助)?\s*(?:生成(?:内容)?|创作)?\s*(?:声明|说明)",
    re.I,
)
_LEGACY_AI_DISCLOSURE_LINE_RE = re.compile(
    r"^[ \t]*(?:本文|本稿)由[ \t]*(?:AI|AIGC|人工智能)[ \t]*(?:基于公开报道[ \t]*)?(?:辅助)?(?:生成|整理撰写|撰写|创作)[^\r\n]*$",
    re.I,
)


def _strip_generated_footer_sections(content_md: str, *, strip_sources: bool = True) -> str:
    """清理旧的自动生成声明；生成/保存时同时移除待重建的来源节。"""
    output: list[str] = []
    removed = False
    skipping_level = 0
    fence_marker = ""
    fence_length = 0
    for line in (content_md or "").splitlines():
        if fence_marker:
            if not skipping_level:
                output.append(line)
            if re.fullmatch(r"[ \t]{0,3}" + re.escape(fence_marker) +
                            "{" + str(fence_length) + r",}[ \t]*", line):
                fence_marker = ""
            continue
        fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            fence_marker, fence_length = fence.group(1)[0], len(fence.group(1))
            if not skipping_level:
                output.append(line)
            continue
        heading = re.match(r"^[ \t]{0,3}(#{1,6})[ \t]*(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            if skipping_level and level > skipping_level:
                continue
            skipping_level = 0
            label = heading.group(2).strip()
            if (label.startswith("声明") or _AI_DISCLOSURE_HEADING_RE.match(label)
                    or (strip_sources and label.startswith("参考来源"))):
                skipping_level = level
                removed = True
                continue
        if not skipping_level:
            label = line.strip().strip("*_")
            ai_label = _AI_DISCLOSURE_HEADING_RE.match(label)
            labelled_disclosure = ai_label and label[ai_label.end():].lstrip().startswith(("：", ":"))
            if _LEGACY_AI_DISCLOSURE_LINE_RE.fullmatch(label) or labelled_disclosure:
                removed = True
            else:
                output.append(line)
    if not strip_sources and not removed:
        return content_md or ""
    return "\n".join(output).rstrip()


def _ensure_source_footer(content_md: str, source_refs: list[dict]) -> str:
    """从不可变来源快照重建参考来源段，不保留模型自行生成的尾注。"""
    text = _strip_generated_footer_sections(content_md)
    source_lines = []
    for item in source_refs:
        title = re.sub(r"[\r\n]+", " ", str(item.get("title") or "")).strip()
        source = re.sub(r"[\r\n]+", " ", str(item.get("source") or "")).strip()
        if title:
            source_lines.append(f"- {source}丨《{title}》")
    if source_lines:
        text += f"\n\n{SOURCES_SECTION_HEADING}\n" + "\n".join(source_lines)
    return text


def generate_article_for_draft(
    db: Session, draft_id: int, trigger: str = "manual", generation: int | None = None,
) -> tuple[bool, str]:
    """为草稿生成文章正文（article_gen_task 的服务层）。

    返回 (是否成功, 错误信息)。成功后草稿行更新为 ready；失败一律 failed
    （含未配置 LLM / SSRF 拦截 / 素材为空 / 两次 JSON 解析失败 / 字段缺失）。
    用量记账 task_type=article：成功 success、API 失败 api_fail、
    JSON 解析失败 parse_fail（token 已花如实落账）。

    并发互斥：pg_try_advisory_xact_lock 按 draft id 串行化——stale 放行重试
    （API 层 15 分钟）与队列积压的组合可能让同一草稿双任务并发，抢不到锁的
    一方直接退出且不改状态（在跑的任务持有所有权，最终会把状态写完）。
    """
    # 先取事务锁再重新读取：旧任务可能排队很久才启动；不能用锁前缓存的
    # generating 状态覆盖已就绪、已被编辑或正在进行下一代生成的草稿。
    if db.bind is None or db.bind.dialect.name == "postgresql":
        acquired = db.execute(
            sql_text("SELECT pg_try_advisory_xact_lock(hashtext('article_draft:' || :i))"),
            {"i": draft_id},
        ).scalar()
        if not acquired:
            return False, "another generation in flight"
    row = db.get(ArticleDraft, draft_id, populate_existing=True)
    if row is None or row.channel != "wechat":
        return False, "draft not found"
    if row.status != "generating" or (generation is not None and row.gen_count != generation):
        return False, "generation superseded"
    expected_generation = row.gen_count
    start = time.monotonic()
    duration_ms = lambda: int((time.monotonic() - start) * 1000)  # noqa: E731 闭包内多处复用

    def _enabled_from_committed_config() -> bool:
        # ORM get() 可能返回 identity map 中的旧配置。只读标量 SQL 直接观察
        # 最新已提交值；未设置 DB 覆盖时才回退到 .env 默认。
        value = db.execute(
            select(SystemSetting.value).where(SystemSetting.key == "article_gen_enabled")
        ).scalar_one_or_none()
        if value is None or value == "":
            return bool(get_settings().article_gen_enabled)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _stop_if_disabled() -> bool:
        if _enabled_from_committed_config():
            return False
        _fail(db, row, "文章生成已在运维面板关闭（article_gen_enabled=false）", duration_ms(), expected_generation)
        return True

    # 队列任务可能在入队后被运维停用。禁用时只标记本代失败，不计任何 LLM 用量。
    if _stop_if_disabled():
        return False, "article generation disabled"

    # 模型选择：热配置 article_gen_model 非空则覆盖主模型（文章 token 远大于摘要，
    # 允许指定便宜模型），协议与密钥仍取主配置
    llm = effective_llm(db)
    override = str(config_service.get_effective(db, "article_gen_model") or "").strip()
    model = override or llm[2]
    base_url, api_key, _, protocol = llm

    def _record(status: str, error: str | None, **tokens) -> None:
        record_usage(
            task_type="article",
            model=model,
            status=status,
            error=error,
            latency_ms=duration_ms(),
            trigger=trigger,
            event_id=row.source_id if row.source_type == "event" else None,
            article_id=row.source_id if row.source_type == "article" else None,
            **tokens,
        )

    if len(model) > 64:
        error = "模型标识超过用量流水支持的 64 字符上限，未调用模型"
        _fail(db, row, error, duration_ms(), expected_generation)
        return False, error
    if not (base_url and api_key and model):
        _fail(db, row, "未配置 LLM（llm_base_url / llm_api_key / llm_model），请在运维面板配置", duration_ms(), expected_generation)
        return False, "llm not configured"
    try:
        base_url = assert_public_http_url(base_url)
    except UrlNotAllowed as exc:
        _record("api_fail", str(exc))
        _fail(db, row, f"LLM 地址被 SSRF 校验拦截：{exc}", duration_ms(), expected_generation)
        return False, str(exc)

    material, event_summary = _collect_material(db, row)
    if not material:
        _fail(db, row, "素材为空：源内容可能已被清理，无法生成", duration_ms(), expected_generation)
        return False, "no material"

    prompt = _build_prompt(row, material, event_summary)
    data = None
    usage: dict = {}
    feedback = ""
    for _attempt in range(2):
        # 每次请求独立持有用量，避免解析失败后的用量污染下一次 API 失败记录。
        usage = {}
        if _stop_if_disabled():
            return False, "article generation disabled"
        try:
            raw, usage = llm_client.chat(
                base_url, api_key, model, protocol, prompt + feedback,
                system=_SYSTEM_PROMPT, temperature=0.4, json_mode=True,
                timeout=CHAT_TIMEOUT_SECONDS,
            )
            data = llm_client.extract_json_object(raw)
            break
        except llm_client.LLMAPIError as exc:
            _record("api_fail", str(exc), **usage)
            _fail(db, row, f"LLM 调用失败：{exc}", duration_ms(), expected_generation)
            return False, str(exc)
        except (ValueError, llm_client.LLMFormatError) as exc:
            # JSON 提取失败：本次 token 如实落账，带纠错反馈重试一次
            usage = getattr(exc, "usage", None) or usage
            _record("parse_fail", str(exc), **usage)
            feedback = llm_client.JSON_RETRY_HINT.format(error=exc)

    if data is None:
        _fail(db, row, "AI 输出连续两次无法解析为 JSON，请重新生成", duration_ms(), expected_generation)
        return False, "parse failed"

    title = _clean_llm_text(data.get("title"))
    content_md = str(data.get("content_md") or "").strip()
    if not title or not content_md:
        _record("parse_fail", "missing title/content_md", **usage)
        _fail(db, row, "AI 输出缺少标题或正文，请重新生成", duration_ms(), expected_generation)
        return False, "missing fields"

    raw_alt_titles = data.get("alt_titles")
    if not isinstance(raw_alt_titles, list):
        raw_alt_titles = []
    alt_titles = [
        truncate(_clean_llm_text(t), TITLE_LIMIT)
        for t in raw_alt_titles[:3]
        if isinstance(t, str) and _clean_llm_text(t)
    ]
    # digest 缺省回退：正文剥 markdown 标记后取首段截断
    digest = _clean_llm_text(data.get("digest")) or _first_paragraph(content_md)

    source_refs = source_refs_from_material(material)
    content_md = _ensure_source_footer(content_md, source_refs)
    _record("success", None, **usage)
    changed = (
        db.query(ArticleDraft)
        .filter(ArticleDraft.id == draft_id, ArticleDraft.channel == "wechat", ArticleDraft.status == "generating",
                ArticleDraft.gen_count == expected_generation)
        .update({
            ArticleDraft.title: truncate(title, TITLE_LIMIT),
            ArticleDraft.alt_titles: alt_titles,
            ArticleDraft.digest: truncate(digest, DIGEST_LIMIT),
            ArticleDraft.content_md: content_md,
            ArticleDraft.source_refs: source_refs,
            ArticleDraft.model: model,
            ArticleDraft.prompt_tokens: int(usage.get("prompt_tokens") or 0),
            ArticleDraft.completion_tokens: int(usage.get("completion_tokens") or 0),
            ArticleDraft.status: "ready",
            ArticleDraft.error: "",
            ArticleDraft.duration_ms: duration_ms(),
            ArticleDraft.revision: ArticleDraft.revision + 1,
        }, synchronize_session=False)
    )
    if not changed:
        db.rollback()
        return False, "generation superseded"
    db.commit()
    return True, ""


_MD_TOKEN_RE = re.compile(r"[#>*`\[\]()！!]")


def _first_paragraph(content_md: str) -> str:
    """正文首个非空非标题段（digest 回退用），剥残留 markdown 标记。"""
    for line in content_md.splitlines():
        text = line.strip()
        if not text or text.startswith(("#", "[配图建议", "-")):
            continue
        return _MD_TOKEN_RE.sub("", text).strip()
    return ""
