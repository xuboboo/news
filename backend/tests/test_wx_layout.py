"""公众号排版草稿域回归测试：每日早报 → 公众号 HTML 排版草稿链路。

离线运行：python -m pytest tests/test_wx_layout.py -q
覆盖：登录门禁、日期校验、无早报 404、空早报 400、排版产物（固定开头/结尾、
正文栏目、XSS 转义）、同日幂等重排版（gen_count/revision 递增）、
列表/详情/重新生成/删除（revision 冲突 409）。
确定性渲染不经 LLM：无需配置模型与队列。
"""
import pytest

from conftest import PW, login

H = {"X-Requested-With": "XMLHttpRequest"}

_BANNER = "https://mmbiz.qpic.cn/sz_mmbiz_gif/2UFeAILAzXlqRWpYBvvLKg6pYFz0EqvksQxEMCcoxncrF0JVVsl6l4dpj3crHvWyRTGbkH1GM41nAKeQmw8TSgfLiaKfiaNd7ibf1w0npYaMNM/640"


def _seed_report(db, date_str="2026-10-14", *, empty=False, evil_title=None):
    from datetime import date

    from app.models.report import DailyReport

    content = {
        "date": date_str,
        "issue_no": 200,
        "overview": "昨夜今晨科技圈热点综述。",
        "top_events": [] if empty else [
            {
                "id": 1,
                "title": evil_title or "事件甲发布新一代旗舰",
                "summary": "事件甲官方发布摘要。",
                "hot_score": 88.8,
                "articles_count": 3,
                "sources_count": 2,
            }
        ],
        "quick_news": [] if empty else [
            {"id": 2, "title": "速览新闻标题", "source": "IT之家", "hot_score": 50},
        ],
        "industry_changes": [] if empty else ["行业变化一"],
        "top_articles": [] if empty else [
            {"id": 3, "title": "热榜文章标题", "source": "快科技", "hot_score": 77.7},
        ],
        "articles_total": 0 if empty else 12,
    }
    s = db.factory()
    s.add(DailyReport(date=date.fromisoformat(date_str), issue_no=200, content=content))
    s.commit()
    s.close()
    return content


@pytest.fixture()
def member_client(client, seeded):
    """以租户 A 成员身份登录的测试客户端（草稿全员共享，member 即可读写）。"""
    resp = login(client, seeded.emails["member"])
    assert resp.status_code == 200
    return client


def test_create_requires_login(client, db):
    _seed_report(db)
    resp = client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H)
    assert resp.status_code == 401


def test_create_rejects_bad_date(member_client, db):
    resp = member_client.post("/api/v2/wx-layouts", json={"date": "2026/10/14"}, headers=H)
    assert resp.status_code == 400


def test_create_missing_report_404(member_client, db):
    resp = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-01"}, headers=H)
    assert resp.status_code == 404


def test_create_empty_report_400(member_client, db):
    _seed_report(db, empty=True)
    resp = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H)
    assert resp.status_code == 400
    assert "为空" in resp.json()["detail"]


def test_create_and_payload(member_client, db):
    _seed_report(db)
    resp = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["regenerated"] is False
    assert body["report_date"] == "2026-10-14"
    assert body["issue_no"] == 200
    assert body["title"] == "今日早报丨10月14日·昨夜今晨科技热点"
    assert body["digest"] == "昨夜今晨科技圈热点综述。"

    detail = member_client.get(f"/api/v2/wx-layouts/{body['id']}").json()
    html_text = detail["content_html"]
    # 固定开头/结尾：横幅、END 徽章、公众号名片、点赞互动区块逐字在位
    assert _BANNER in html_text
    assert "<strong data-brushtype=\"text\">END</strong>" in html_text
    assert 'data-nickname="华军AI产品榜"' in html_text
    assert "点点赞" in html_text and "点分享" in html_text and "点喜欢" in html_text
    assert '<mp-style-type data-value="3">' in html_text
    # 正文栏目与风格
    assert "昨夜今晨热点事件" in html_text
    assert "事件甲发布新一代旗舰" in html_text
    assert "行业变化一" in html_text
    assert "速览新闻标题（IT之家）" in html_text
    assert "热榜文章标题（快科技）" in html_text
    assert 'font-size: 15px' in html_text
    # 不携带站点主题痕迹、无脚本
    assert "<script" not in html_text


def test_xss_escaped(member_client, db):
    _seed_report(db, evil_title='<b>加粗</b><script>alert(1)</script>"引号')
    resp = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H)
    assert resp.status_code == 200
    detail = member_client.get(f"/api/v2/wx-layouts/{resp.json()['id']}").json()
    html_text = detail["content_html"]
    assert "<script>" not in html_text
    assert "<b>加粗</b>" not in html_text
    assert "&lt;b&gt;加粗&lt;/b&gt;" in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text


def test_regenerate_same_day_is_upsert(member_client, db):
    _seed_report(db)
    first = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H).json()
    second = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H).json()
    assert second["regenerated"] is True
    assert second["id"] == first["id"]
    assert second["gen_count"] == first["gen_count"] + 1
    assert second["revision"] == first["revision"] + 1
    # 列表仍只有一份草稿
    listing = member_client.get("/api/v2/wx-layouts").json()
    assert listing["total"] == 1


def test_regenerate_endpoint_and_missing_source(member_client, db):
    _seed_report(db)
    created = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H).json()
    resp = member_client.post(f"/api/v2/wx-layouts/{created['id']}/regenerate", json={}, headers=H)
    assert resp.status_code == 200
    assert resp.json()["gen_count"] == created["gen_count"] + 1

    # 源早报被清理后：重新生成 404
    from app.models.report import DailyReport
    s = db.factory()
    s.query(DailyReport).filter(DailyReport.date == "2026-10-14").delete()
    s.commit()
    s.close()
    resp = member_client.post(f"/api/v2/wx-layouts/{created['id']}/regenerate", json={}, headers=H)
    assert resp.status_code == 404


def test_delete_revision_guard(member_client, db):
    _seed_report(db)
    created = member_client.post("/api/v2/wx-layouts", json={"date": "2026-10-14"}, headers=H).json()
    draft_id = created["id"]

    missing = member_client.request("DELETE", f"/api/v2/wx-layouts/{draft_id}", json={}, headers=H)
    assert missing.status_code == 428

    stale = member_client.request(
        "DELETE", f"/api/v2/wx-layouts/{draft_id}", json={"revision": created["revision"] + 5}, headers=H
    )
    assert stale.status_code == 409

    ok = member_client.request(
        "DELETE", f"/api/v2/wx-layouts/{draft_id}", json={"revision": created["revision"]}, headers=H
    )
    assert ok.status_code == 200
    assert member_client.get(f"/api/v2/wx-layouts/{draft_id}").status_code == 404
