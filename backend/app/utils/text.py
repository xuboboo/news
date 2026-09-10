"""中文文本处理：分词、关键词、句子切分（jieba 驱动）。"""
import re
from typing import List

import jieba
import jieba.analyse

jieba.setLogLevel(60)  # 关闭初始化日志

_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")
_TAG_STRIP = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(html: str) -> str:
    """剥离 HTML 标签，保留正文文本。"""
    if not html:
        return ""
    text = _TAG_STRIP.sub(" ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
    return _WS.sub(" ", text).strip()


def split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "")]
    return [p for p in parts if len(p) >= 6]


# 站点套话/元信息（非正文事实），摘要与要点抽取时过滤。
# 注意：不要把「IT之家 X 月 X 日消息」整句过滤——该句通常还承载导语事实。
_BOILERPLATE = re.compile(r"^(感谢|题图|配图|图片来源|广告声明)")


def is_boilerplate(sentence: str) -> bool:
    return bool(_BOILERPLATE.match(sentence.strip()))


def tokenize(text: str) -> List[str]:
    return [w for w in jieba.cut(text or "") if len(w) > 1 and not re.match(r"^[\W\d_]+$", w)]


def extract_keywords(text: str, topk: int = 8) -> List[str]:
    """TF-IDF 关键词抽取（jieba 内置算法）。"""
    if not text:
        return []
    tags = jieba.analyse.extract_tags(text, topK=topk)
    return [t for t in tags if len(t) > 1]


def truncate(text: str, limit: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def truncate_boundary(text: str, limit: int = 64) -> str:
    """展示用截断：超限时优先在句内最后一个逗号/顿号处收尾，避免"将会基…"半字硬切。

    后半段（limit 的 40% 之后）找不到标点时退化为普通硬截断；
    截点回退避开英文/数字单词中部（"共塑 Wi…" → "共塑 …"）。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("，", limit // 2, limit - 1)
    if cut <= 0:
        cut = text.rfind("、", limit // 2, limit - 1)
    if cut <= 0:
        cut = limit - 1
    # 回退避开 ASCII 单词中部
    while cut > 0 and text[cut - 1].isascii() and text[cut].isascii() and text[cut - 1].isalnum():
        cut -= 1
    return text[:cut] + "…"
