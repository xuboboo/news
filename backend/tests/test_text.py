"""文本工具单测：分句 / 套话过滤 / 截断 / 关键词。"""
from app.utils.text import (
    extract_keywords,
    is_boilerplate,
    split_sentences,
    strip_html,
    truncate,
)


def test_split_sentences_filters_short_fragments():
    text = "苹果发布新款MacBook Pro笔记本。续航达到二十四小时之久！短"
    assert split_sentences(text) == ["苹果发布新款MacBook Pro笔记本", "续航达到二十四小时之久"]


def test_split_sentences_empty():
    assert split_sentences("") == []


def test_is_boilerplate():
    assert is_boilerplate("感谢投递，线索来自读者")
    assert is_boilerplate("题图：官方发布会截图")
    assert is_boilerplate("广告声明：本文含推广链接")
    assert not is_boilerplate("苹果公司今日发布新款手机")


def test_truncate():
    assert truncate("短文本", 60) == "短文本"
    out = truncate("长" * 80, 10)
    assert len(out) == 10 and out.endswith("…")


def test_strip_html():
    assert strip_html("<p>hello&nbsp;world</p>") == "hello world"
    assert strip_html("") == ""


def test_extract_keywords_smoke():
    kws = extract_keywords("苹果公司发布搭载M5芯片的新款MacBook Pro，续航创历史新高", 5)
    assert 0 < len(kws) <= 5
    assert all(len(t) > 1 for t in kws)
    assert extract_keywords("", 5) == []
