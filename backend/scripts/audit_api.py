"""验收 F：API 全端点遍历（正常 + 参数 + 404 异常路径）。

2026-09 起全站强制登录：数据端点需有效会话。脚本以 AUDIT_EMAIL/AUDIT_PASSWORD
（环境变量，须为平台超管或普通成员均可）登录建立 Cookie 会话后再遍历；
未配置时仅执行公开端点（health）与鉴权/CORS 用例。
"""
import json
import os
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:18000/api/v2"
AUDIT_EMAIL = os.environ.get("AUDIT_EMAIL", "")
AUDIT_PASSWORD = os.environ.get("AUDIT_PASSWORD", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
results = []

# 会话画像：CookieJar 自动管理 hjradar_session
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))

_AUTH_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def call(name, path, expect=200, method="GET"):
    try:
        safe = urllib.parse.quote(path, safe="/?=&")
        req = urllib.request.Request(BASE + safe, method=method, headers=dict(_AUTH_HEADERS))
        with OPENER.open(req, timeout=15) as r:
            status, body = r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode("utf-8")
    ok = status == expect
    detail = ""
    try:
        data = json.loads(body)
    except Exception:
        data = None
    results.append((name, path, status, "PASS" if ok else "FAIL", detail))
    return data


def check(name, cond, detail=""):
    results.append(("", name, "", "PASS" if cond else "FAIL", detail))


# ———— 会话建立：health 公开，其余数据端点需登录 ————
d = call("健康检查", "/health")
check("health.status=ok", d and d.get("status") == "ok")

LOGGED_IN = False
if AUDIT_EMAIL and AUDIT_PASSWORD:
    req = urllib.request.Request(
        BASE + "/auth/login",
        data=json.dumps({"email": AUDIT_EMAIL, "password": AUDIT_PASSWORD}).encode(),
        headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        method="POST",
    )
    try:
        with OPENER.open(req, timeout=15) as r:
            LOGGED_IN = r.status == 200
    except urllib.error.HTTPError:
        LOGGED_IN = False
    check("登录建立会话", LOGGED_IN, "检查 AUDIT_EMAIL / AUDIT_PASSWORD 是否正确")
else:
    check("未配置 AUDIT_EMAIL/AUDIT_PASSWORD", False,
          "设 AUDIT_EMAIL / AUDIT_PASSWORD 环境变量后重跑（数据端点用例将失败）")

if LOGGED_IN:
    # 系统统计
    d = call("系统统计", "/stats")
    check("stats 字段齐全", d and all(k in d for k in
          ["articles_total", "articles_summarized", "events_total", "ai_coverage", "last_crawl"]))
    check("AI 覆盖率=100%", d and d["ai_coverage"] == 100.0, str(d and d["ai_coverage"]))

    # 热榜
    d = call("热榜(全部)", "/hot/board")
    check("热榜非空", d and len(d["items"]) > 0)
    check("热榜按热度排序", d and all(
        d["items"][i]["hot_score"] >= d["items"][i + 1]["hot_score"] for i in range(len(d["items"]) - 1)))
    check("榜单含摘要/事件关联", d and all("summary" in it for it in d["items"]))
    d = call("热榜(频道筛选)", "/hot/board?channel=软件之家")
    check("频道筛选生效", d and all(it["channel"] == "软件之家" for it in d["items"]), f"{len(d and d['items'])} 条")

    # 最新资讯
    d = call("最新资讯", "/articles/latest?limit=100")
    check("资讯非空", d and len(d["items"]) > 0)
    check("按时间倒序", d and all(
        d["items"][i]["publish_time"] >= d["items"][i + 1]["publish_time"] for i in range(len(d["items"]) - 1)))
    total = d["total"] if d else 0
    d = call("资讯搜索", "/articles/latest?q=AI")
    check("搜索过滤生效", d and d["total"] < total and all("AI" in it["title"] or "ai" in it["title"].lower() for it in d["items"]),
          f"{total}->{d['total'] if d else 0}")
    d = call("资讯频道+搜索组合", "/articles/latest?channel=智能时代&limit=5")
    check("组合筛选", d and all(it["channel"] == "智能时代" for it in d["items"]))

    # 文章与摘要
    aid = (call("取一篇文章ID", "/articles/latest?limit=1") or {}).get("items", [{}])[0].get("id")
    d = call("文章详情", f"/article/{aid}")
    check("详情含原文URL", d and d.get("url", "").startswith("http"))
    d = call("AI摘要(PRD 12)", f"/article/{aid}/summary")
    check("摘要三要素齐全", d and d.get("summary") and len(d.get("points", [])) >= 1 and len(d.get("tags", [])) >= 1,
          f"points={len(d.get('points', []))} tags={len(d.get('tags', []))}")
    call("不存在的文章→404", "/article/999999", expect=404)
    call("不存在摘要→404", "/article/999999/summary", expect=404)

    # 事件
    d = call("热点事件(PRD 12)", "/events")
    check("事件非空", d and len(d["items"]) > 0)
    ev_items = (d or {}).get("items", [])
    ev = ev_items[0] if ev_items else {}
    check("事件字段齐全", all(k in ev for k in ["title", "summary", "status", "hot_score", "timeline", "articles"]))
    check("事件按热度排序", all(
        ev_items[i]["hot_score"] >= ev_items[i + 1]["hot_score"] for i in range(len(ev_items) - 1)))
    eid = ev.get("id")
    d = call("事件详情", f"/events/{eid}")
    check("详情含相关报道+时间线", d and len(d.get("articles", [])) > 0 and len(d.get("timeline", [])) > 0)
    merged = max(ev_items, key=lambda e: e.get("articles_count", 0), default=None)
    check("存在多来源聚合事件", merged is not None and merged["articles_count"] >= 2,
          f"最大事件聚合 {merged.get('articles_count') if merged else 0} 篇")
    call("不存在事件→404", "/events/999999", expect=404)

    # 日报
    d = call("日报(最新)", "/report/daily")
    check("日报五要素齐全", d and all(d.get(k) is not None for k in
          ["top_events", "quick_news", "industry_changes", "source_stats", "top_articles"]))
    check("日报热度非全零", d and any(e.get("hot_score", 0) > 0 for e in d.get("top_events", [])))
    date = d["date"] if d else ""
    call("日报(指定日期)", f"/report/daily?date={date}")
    call("不存在的日期→404", "/report/daily?date=2025-01-01", expect=404)
    d = call("日报日期列表", "/report/dates")
    check("日期列表非空", d and len(d["items"]) > 0)

    # ———— 参数校验（2026-09 修复回归） ————
    call("offset 负数→422", "/articles/latest?offset=-1", expect=422)
    call("日报日期非法→400", "/report/daily?date=not-a-date", expect=400)

    # 事件列表/详情热度口径一致
    d = call("事件热度口径", "/events")
    ev_items = (d or {}).get("items", [])
    if ev_items:
        eid0 = ev_items[0]["id"]
        dd = call("事件详情口径对比", f"/events/{eid0}")
        check("同一事件列表/详情分数一致", dd and dd.get("hot_score") == ev_items[0]["hot_score"],
              f"list={ev_items[0]['hot_score']} detail={dd and dd.get('hot_score')}")
else:
    check("数据端点用例跳过（未登录）", True, "登录建立会话后重跑以覆盖数据端点")


def call_with_headers(name, path, headers=None, expect=200, method="GET", body=None):
    """带自定义请求头的调用（管理接口鉴权 / CSRF / CORS 预检用）。返回 (status, body, headers)。"""
    try:
        req = urllib.request.Request(BASE + urllib.parse.quote(path, safe="/?=&"), method=method,
                                     data=body.encode() if body else None)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), dict(e.headers)


# 3) 管理接口鉴权（要求后端 ADMIN_NO_AUTH=false）
s, _, _ = call_with_headers("admin 无token", "/admin/config")
check("admin 无 token → 401", s == 401, f"status={s}")

if ADMIN_TOKEN:
    # 4) 带 token 正常访问 + 敏感键明文不回传
    s, body, _ = call_with_headers("admin 带token", "/admin/config", headers={"X-Admin-Token": ADMIN_TOKEN})
    check("admin 带 token → 200", s == 200, f"status={s}")
    try:
        cfg = json.loads(body)
        sensitive = set(cfg.get("sensitive", []))
        leak = [k for k in sensitive if (cfg["config"].get(k, {}).get("raw") not in (None, ""))]
        check("敏感键 raw 不回传明文", not leak, f"泄漏字段: {leak}")
    except Exception as exc:
        check("admin config JSON 可解析", False, str(exc))

    # 5) SSRF 拦截：私网地址必须 400
    s, body, _ = call_with_headers(
        "test/llm 私网地址", "/admin/test/llm",
        headers={"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
        method="POST", body=json.dumps({"base_url": "http://127.0.0.1:9999/v1", "api_key": "x", "model": "m"}),
    )
    check("test/llm 私网地址 → 400", s == 400, f"status={s} body={body[:120]}")
else:
    check("ADMIN_TOKEN 未设置，跳过带 token 用例", True, "设 ADMIN_TOKEN 环境变量后重跑")

# 6) CORS：非白名单来源不得获得 ACAO 头
s, _, hdrs = call_with_headers(
    "CORS 预检-恶意来源", "/hot/board",
    headers={"Origin": "https://evil.example",
             "Access-Control-Request-Method": "GET"},
    method="OPTIONS",
)
acao = hdrs.get("access-control-allow-origin") or hdrs.get("Access-Control-Allow-Origin")
check("异源预检无 ACAO 放行", acao is None, f"acao={acao}")
s, _, hdrs = call_with_headers("CORS 预检-白名单来源", "/hot/board",
                               headers={"Origin": "http://localhost",
                                        "Access-Control-Request-Method": "GET"},
                               method="OPTIONS")
acao = hdrs.get("access-control-allow-origin") or hdrs.get("Access-Control-Allow-Origin")
check("白名单来源 ACAO 正常", acao == "http://localhost", f"acao={acao}")

# 汇总
print("\n===== API 验收结果 =====")
passed = failed = 0
for name, path, status, verdict, detail in results:
    if verdict == "PASS":
        passed += 1
    else:
        failed += 1
        print(f"FAIL | {name} {path} status={status} {detail}")
print(f"PASS {passed} / FAIL {failed} / TOTAL {passed + failed}")
