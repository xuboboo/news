"""共享的文章草稿人工编辑与生成输出规则。"""
import re

TITLE_LIMIT = 30
DIGEST_LIMIT = 120
SITE_TITLE_LIMIT = 80
SITE_DIGEST_LIMIT = 160
SITE_KEYWORDS_LIMIT = 160
SITE_TAGS_LIMIT = 120
SITE_COVER_GUIDANCE_LIMIT = 240


def source_refs_from_material(material: list[dict]) -> list[dict[str, str]]:
    """取生成时实际使用的来源快照，不依赖后续文章/事件是否仍存在。"""
    refs: list[dict[str, str]] = []
    for item in material:
        title = re.sub(r"[\r\n]+", " ", str(item.get("title") or "")).strip()
        source = re.sub(r"[\r\n]+", " ", str(item.get("source") or "")).strip()
        if title:
            refs.append({"source": source, "title": title})
    return refs


def source_refs_from_content(content_md: str | None) -> list[dict[str, str]]:
    """从旧草稿已有的标准参考来源小节提取兼容性来源快照。"""
    text = content_md or ""
    heading = re.search(
        r"(?im)^[ \t]{0,3}#{1,6}[ \t]*参考来源[^\r\n]*$",
        text,
    )
    if not heading:
        return []
    following = re.search(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", text[heading.end():])
    section_end = heading.end() + following.start() if following else len(text)
    refs: list[dict[str, str]] = []
    for line in text[heading.end():section_end].splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        value = line[1:].strip()
        source, marker, rest = value.partition("丨《")
        if not marker or not rest.endswith("》"):
            continue
        title = rest[:-1].strip()
        if title:
            refs.append({"source": source.strip(), "title": title})
    return refs
