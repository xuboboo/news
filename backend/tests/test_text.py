"""文本工具单测：分句 / 套话过滤 / 截断 / 关键词 / 媒体导语壳剥离。"""
from app.utils.text import (
    extract_keywords,
    is_boilerplate,
    split_sentences,
    strip_html,
    strip_media_lede,
    truncate,
)


def test_split_sentences_filters_short_fragments():
    text = "苹果发布新款MacBook Pro笔记本。续航达到二十四小时之久！短"
    assert split_sentences(text) == ["苹果发布新款MacBook Pro笔记本", "续航达到二十四小时之久"]


def test_split_sentences_empty():
    assert split_sentences("") == []


def test_strip_media_lede():
    """媒体导语壳剥离：日期导语 / 据X消息·报道转述壳 / 时间状语 / 无壳原样返回（含空值）。"""
    assert strip_media_lede("IT之家 9 月 8 日消息，科技媒体 X 报道称微软调整。") == "科技媒体 X 报道称微软调整。"
    assert strip_media_lede("快科技 2026 年 9 月 9 日讯，某厂发布新处理器。") == "某厂发布新处理器。"
    assert strip_media_lede("据韩媒 sedaily 消息，三星加快研发。") == "三星加快研发。"
    assert strip_media_lede("据《华尔街日报》今日报道，谷歌确认细节。") == "谷歌确认细节。"
    assert strip_media_lede("在今天（18 日）的第 22 届论坛上，专家指出趋势。") == "第 22 届论坛上，专家指出趋势。"
    assert strip_media_lede("昨天的发布会上推出了新品。") == "发布会上推出了新品。"
    assert strip_media_lede("普通正文开头") == "普通正文开头"
    assert strip_media_lede("") == ""


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
