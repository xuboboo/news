"""微信公众号早报排版服务：早报内容快照 → 公众号后台可直接粘贴的内联样式 HTML。

固定的是「开头 / 结尾」两块品牌模板：开头为「找软件用软件，就到华军软件园」
分隔线，结尾为 END 徽章 + 公众号名片 + 点赞互动区块（运营在 135 编辑器定稿，
素材图挂 mmbiz.qpic.cn，粘贴进公众号后台时由编辑器自动转存）。
正文只是内容载体：字体/颜色/间距用站点默认简洁排版（15px、1.6 行高、加粗
小节标题），不锁定 135 样例格式，运营可在后台随意再调整。

确定性渲染，不经 LLM：早报内容本身已是策展结果（综述/事件/速览/热榜），
排版只负责组合与样式——零 token 成本、输出可预期、生产未配 LLM 也可用。
早报综述若为 AI 撰写（overview_source=llm），文章导语自然继承 AI 文案。

安全口径：早报内容汇聚自爬取的资讯标题/摘要（不可信输入），所有动态文本
一律经 HTML 转义后进入正文，杜绝把外站标题当成标签注入文章。
"""
import html
import re

# ———————————————————— 固定开头：「找软件用软件」分隔线（左右灰线 + 双竖条 + 蓝字） ————————————————————
_BAR = (
    '<section style="width: 3px;height: 14px;background-color: #333333;{margin}box-sizing:border-box;" '
    'data-ignore-width><p><br/></p></section>'
)
_HEADER_TICKS_LEFT = _BAR.format(margin="margin-right: 4px;") + _BAR.format(margin="margin-right: 12px;")
_HEADER_TICKS_RIGHT = _BAR.format(margin="margin-left: 12px;") + _BAR.format(margin="margin-left: 4px;")
_HEADER_DIVIDER = (
    '<section style="margin: 10px auto;display: flex;justify-content: center;align-items: center;">'
    '<section style="flex: 1 1 0%;height: 1px;background-color: #a6a6a6;" data-ignore-width><p><br/></p></section>'
    '<section style="flex-shrink: 0;display: flex;align-items: center;margin: 0 10px;">'
    f"{_HEADER_TICKS_LEFT}"
    '<section style="font-size: 15px;color: #4a90d9;text-align: center;box-sizing:border-box;">'
    '<strong data-brushtype="text">找软件用软件，就到华军软件园</strong>'
    "</section>"
    f"{_HEADER_TICKS_RIGHT}"
    "</section>"
    '<section style="flex: 1 1 0%;height: 1px;background-color: #a6a6a6;" data-ignore-width><p><br/></p></section>'
    "</section>"
)

# ———————————————————— 固定结尾：END 徽章（135 编辑器 data-id=178195） ————————————————————
_END_TICKS = "".join(
    '<section style="width: 14px;height: 1px;border-top: 1px solid #ffffff;'
    + ("box-sizing:border-box;" if i == 0 else "margin-top:2px;box-sizing:border-box;")
    + '" data-ignore-width><p><br/></p></section>'
    for i in range(3)
)
_END_BADGE = (
    '<section data-tools="135编辑器" data-id="178195" class="_135editor">'
    '<section style="margin: 10px auto;display: flex;justify-content: center;">'
    '<section style="border-radius: 25px;background-color: #2680e8;padding-bottom: 2px;box-sizing:border-box;">'
    '<section style="border-radius: 25px;background-color: #54a3ff;padding: 5px 15px;box-sizing:border-box;">'
    '<section style="display: flex;justify-content: space-between;align-items: center;">'
    f'<section style="flex-shrink: 0;">{_END_TICKS}</section>'
    '<section style="margin: 0 14px;">'
    '<section style="font-size: 16px;color: #ffffff;text-align: center;padding: 0 10px;box-sizing:border-box;">'
    '<strong data-brushtype="text">END</strong>'
    "</section></section>"
    f'<section style="flex-shrink: 0;">{_END_TICKS}</section>'
    "</section></section></section></section>"
    "</section>"
)

# 公众号名片（mp-common-profile 为公众号原生组件，粘贴进后台自动识别）
_PROFILE_CARD = (
    '<section class="js_uneditable custom_select_card mp_profile_iframe _135editor">'
    '<mp-common-profile data-pluginname="mpprofile" data-nickname="华军AI产品榜" data-alias="AIRank-CN" '
    'data-from="0" data-headimg="https://mmbiz.qlogo.cn/sz_mmbiz_png/M4x9RFS4SlsviaDoO04CafFNDOkUfniaNul7MncuiaxFCy4saUeY7xSwiapwQBz506w0oemFeTPej5fReiaZmDIaXkg/0?wx_fmt=png" '
    'data-signature="华军软件园出品，每月发布国内AI产品排行数据丨发现最受欢迎的先进工具丨统计维度业内最多" '
    'data-id="Mzg5OTk4MDU2NQ==" data-is_biz_ban="0" data-service_type="1" data-verify_status="2">'
    "</mp-common-profile></section>"
)

# 点赞/分享/喜欢互动区块（135 编辑器 data-id=157683）
_INTERACT_ITEMS = (
    (
        "https://mmbiz.qpic.cn/sz_mmbiz_gif/vqyAuPymgXHamXFlK2y7PomTsPCLvhRVre8icnTvkeMPlDBnKHYxWfonJPghaVPYZiaY0CNBGthy7VGqG2ApskRcia9CHDPR6oqwDMshuXrPxQ/640?wx_fmt=gif&amp;from=appmsg",
        "1.098360655737705",
        "122",
        "点点赞",
    ),
    (
        "https://mmbiz.qpic.cn/mmbiz_gif/vqyAuPymgXFfNgnLVarQ3FraicDMOHibpT5ftJe8WNpVZol5TpXpNyo9WRlAlBibTwgDN9VRsX3pUzHibLgZzibFpZoTYern3fibGG8Zx1SKP4Y8s/640?wx_fmt=gif&amp;from=appmsg",
        "1.0634920634920635",
        "126",
        "点分享",
    ),
    (
        "https://mmbiz.qpic.cn/mmbiz_gif/vqyAuPymgXGJ9IR6GJqNAk2bDllRJsElMExibVjmTUk9yicMHq0d6mibtkXK7a3z06Z6gORib7ibIYiaK0Mup6Vo3kvyfkoA4KZFun2nFmSYBx5fk/640?wx_fmt=gif&amp;from=appmsg",
        "1.1166666666666667",
        "120",
        "点喜欢",
    ),
)
_INTERACT_BLOCKS = "".join(
    (f'<section style="margin-left: 20px;">' if i else "<section>")
    + '<section style="width: 35px;margin-bottom: 3px;margin-left: auto;margin-right: auto;box-sizing:border-box;" data-ignore-width>'
    + f'<img src="{src}" class="rich_pages wxw-img" data-type="gif" '
    + 'style="width: 100%;display: block;vertical-align:baseline;box-sizing:border-box;max-width:100% !important;" '
    + f'data-width="100%" draggable="false" data-ratio="{ratio}" data-w="{w}"/>'
    + "</section>"
    + '<section style="font-size: 12px;color: #333333;text-align: center;">'
    + f'<strong data-brushtype="text">{label}</strong>'
    + "</section></section>"
    for i, (src, ratio, w, label) in enumerate(_INTERACT_ITEMS)
)
_INTERACT_ROW = (
    '<section data-tools="135编辑器" data-id="157683" class="_135editor">'
    '<section style="margin: 10px auto;display: flex;justify-content: flex-end;align-items: flex-end;">'
    f"{_INTERACT_BLOCKS}"
    "</section></section>"
)

_PASTE_TAIL = (
    '<p style="display: none;"><br/></p>'
    '<section class="_135editor"><mp-style-type data-value="3"></mp-style-type></section>'
    "<p><br/></p>"
)

# ———————————————————— 正文风格（运营定稿：15px / 1.6 行高 / 加粗小节标题） ————————————————————
_P = '<p style="line-height:1.6;"><span style="font-size: 15px;">{}</span></p>'
_P_META = '<p style="line-height:1.6;"><span style="font-size: 12px;color:#888888;">{}</span></p>'
_H1 = '<h1 style="line-height:1.6;font-size:17px;"><span style="font-size:15px;">{}</span></h1>'
_H2 = '<h2 style="line-height:1.6;font-size:17px;"><span style="font-size:15px;"><strong>{}</strong></span></h2>'
_BR = "<p><br/></p>"

_WS_RE = re.compile(r"\s+")


def _esc(value: object) -> str:
    """动态文本 → 正文安全文本：压平空白 + HTML 转义（早报内容含外站标题，不可信）。"""
    text = _WS_RE.sub(" ", str(value or "")).strip()
    return html.escape(text, quote=False)


def _score_text(raw: object) -> str:
    try:
        score = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return f"{score:g}"


def _section_title(text: str) -> str:
    return _H2.format(_esc(text)) + _BR


def render_report_body(content: dict) -> str:
    """早报内容 JSON → 正文 HTML 片段（不含固定开头/结尾）。

    栏目取自 build_report 快照键（top_events / industry_changes /
    quick_news / top_articles），全部缺省容忍：某一栏目为空即跳过，
    四栏全空抛 ValueError（API 层转 400，宁可不产稿也不出空壳文章）。
    """
    top_events = content.get("top_events") or []
    quick_news = content.get("quick_news") or []
    industry_changes = content.get("industry_changes") or []
    top_articles = content.get("top_articles") or []
    if not (top_events or quick_news or industry_changes or top_articles):
        raise ValueError("早报内容为空")

    blocks: list[str] = []
    date_text = _esc(content.get("date") or "")
    md = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_text)
    date_label = f"{md.group(2)}月{md.group(3)}日" if md else date_text
    issue_no = content.get("issue_no") or 1
    blocks.append(
        _H1.format(
            f"早上好！这里是<strong>华军软件园·今日早报</strong>（{date_label} 第{_esc(issue_no)}期），"
            "昨夜今晨科技圈的大事都在这里。"
        )
    )
    blocks.append(_BR)

    overview = _esc(content.get("overview") or "")
    if overview:
        blocks.append(_P.format(overview))
        blocks.append(_BR)

    if top_events:
        blocks.append(_section_title("昨夜今晨热点事件"))
        for i, ev in enumerate(top_events, 1):
            blocks.append(_P.format(f"<strong>{i}. {_esc(ev.get('title'))}</strong>"))
            summary = _esc(ev.get("summary") or "")
            if summary:
                blocks.append(_P.format(summary))
            meta = []
            score = _score_text(ev.get("hot_score"))
            if score:
                meta.append(f"热度 {score}")
            count = ev.get("articles_count")
            if isinstance(count, int) and count > 0:
                meta.append(f"{count} 篇报道")
            sources = ev.get("sources_count")
            if isinstance(sources, int) and sources > 1:
                meta.append(f"{sources} 家来源交叉")
            if meta:
                blocks.append(_P_META.format(" ｜ ".join(meta)))
            blocks.append(_BR)

    if industry_changes:
        blocks.append(_section_title("行业变化"))
        for change in industry_changes:
            text = _esc(change)
            if text:
                blocks.append(_P.format(f"· {text}"))
        blocks.append(_BR)

    if quick_news:
        blocks.append(_section_title("热点速览"))
        for item in quick_news:
            title = _esc(item.get("title"))
            if not title:
                continue
            source = _esc(item.get("source") or "")
            blocks.append(_P.format(f"· {title}" + (f"（{source}）" if source else "")))
        blocks.append(_BR)

    if top_articles:
        blocks.append(_section_title("昨夜今晨热榜 TOP 10"))
        for i, item in enumerate(top_articles, 1):
            title = _esc(item.get("title"))
            if not title:
                continue
            source = _esc(item.get("source") or "")
            blocks.append(_P.format(f"<strong>{i}.</strong> {title}" + (f"（{source}）" if source else "")))
        blocks.append(_BR)

    return "".join(blocks)


def build_layout(content: dict) -> dict:
    """早报内容 → {"title", "digest", "content_html"}。

    title 为公众号后台单独填写的标题（事实型，不标题党，与全站写作铁律一致）；
    digest 取早报综述（截 120 字，公众号摘要上限）。抛 ValueError 表示内容为空。
    """
    body = render_report_body(content)
    raw_date = str(content.get("date") or "")
    md = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw_date)
    date_label = f"{md.group(2)}月{md.group(3)}日" if md else raw_date
    title = f"今日早报丨{date_label}·昨夜今晨科技热点"

    overview = _WS_RE.sub(" ", str(content.get("overview") or "")).strip()
    digest = overview[:120]

    content_html = (
        '<section data-role="outer">'
        + _HEADER_DIVIDER
        + "<p><br/></p>"
        + '<section data-role="paragraph">'
        + body
        + "</section>"
        + "<p><br/></p>"
        + _END_BADGE
        + "<p><br/></p>"
        + _PROFILE_CARD
        + "<p><br/></p>"
        + _INTERACT_ROW
        + _PASTE_TAIL
        + "</section>"
    )
    return {"title": title, "digest": digest, "content_html": content_html}
