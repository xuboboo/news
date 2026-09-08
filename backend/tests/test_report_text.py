"""日报「行业变化」文案清洗（_clean_change_text / _dedupe_texts）单测：纯函数无 DB。"""
from app.services.report_service import _clean_change_text, _dedupe_texts


def test_strips_it_home_lede_prefix():
    raw = "IT之家 9 月 8 日消息，科技媒体 Windows Latest 今天（9 月 8 日）发布博文，报道称微软高管承诺听取反馈。"
    assert _clean_change_text(raw) == "微软高管承诺听取反馈"


def test_cuts_reporting_connector_within_first_sentence():
    raw = "科技媒体 Windows Central 昨日（9 月 7 日）发布博文，指出 Project Notch 将支持高刷屏切换。"
    assert _clean_change_text(raw).startswith("Project Notch")


def test_connector_in_second_sentence_does_not_hijack():
    raw = "苹果下调营收指引。库克表示需求疲软。补充" + "说明" * 60
    assert _clean_change_text(raw).startswith("苹果下调营收指引")


def test_strips_kj_xun_prefix():
    raw = "快科技 9 月 9 日讯，某厂发布新款处理器，性能提升明显。后续内容不属于首句。"
    out = _clean_change_text(raw)
    assert out == "某厂发布新款处理器，性能提升明显"


def test_keeps_text_without_lede():
    assert _clean_change_text("微软发布 Win11 预览版") == "微软发布 Win11 预览版"
    assert _clean_change_text("") == ""
    assert _clean_change_text(None) == ""


def test_first_sentence_preference():
    long = "苹果下调营收指引。库克表示需求疲软。" + "补充" * 60
    out = _clean_change_text(long)
    assert out == "苹果下调营收指引"


def test_short_first_sentence_falls_back_to_full_text():
    raw = "利好。某公司宣布全面转向 AI 优先战略，并且详细说明了后续三个季度的执行路线图"
    assert _clean_change_text(raw) == raw


def test_strips_ju_message_prefix():
    raw = "据韩媒 sedaily 消息，三星电子正加快其人形机器人原型机的研发。后续内容不属于首句。"
    assert _clean_change_text(raw) == "三星电子正加快其人形机器人原型机的研发"


def test_truncation_prefers_comma_boundary():
    body = "微软高管承诺将会基于用户反馈调整任务栏设计" + "，并且给出后续三个版本的改进路线" * 7
    out = _clean_change_text(body)
    assert len(out) <= 110
    assert out.endswith("…")
    assert out[:-1].endswith("线")  # 在最后一个逗号处截断而非半字硬切


def test_long_text_capped():
    raw = "这是一条没有任何句读的超长文本" + "，继续延续" * 30
    out = _clean_change_text(raw)
    assert len(out) <= 110
    assert out.endswith("…")


def test_dedupe_near_identical():
    items = [
        "科技媒体 Windows Central 发布博文称 Notchify 更新",
        "科技媒体 Windows Central 发布博文称 Notchify 更新，补充细节",
        "火狐 Firefox 157 将默认启用 Nova 新界面",
    ]
    out = _dedupe_texts(items)
    assert len(out) == 2
    assert out[0].startswith("科技媒体")
    assert out[1].startswith("火狐")
