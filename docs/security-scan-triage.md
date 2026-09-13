# 安全扫描告警分诊记录

Mimosa 提交钩子对全仓库做模式扫描，以下 12 条 high 告警经人工逐条核实为
**误报**（2026-09-14，随 `42ffb8e`/`2754efb` 提交时首次系统性分诊）。
决策：**不修改代码**，本文档作为豁免依据，后续扫描复现时直接引用，勿重复排查。

## SSRF（6 条）—— 运维审计/维护脚本，目标为自家 API 或写死的站点常量

| 位置 | 实际用途 |
|---|---|
| `backend/scripts/audit_auth.py:121,131,288` | 安全审计脚本验证 401/403/CSRF 防线，`urlopen(BASE+...)` 的 BASE 为脚本内常量，无外部输入 |
| `backend/scripts/audit_api.py:170` | 同上，API 审计脚本的请求封装 |
| `backend/scripts/audit_sources.py:31` | 来源核对脚本，抓取 `PAGE_SOURCES` 字典中写死的站点页面 |
| `backend/scripts/rechannel_articles.py:33` | 文章改频道维护脚本，同上 |

SSRF 成立的前提是「攻击者可控输入到达服务端请求」；以上均为运维手动运行的
一次性工具，URL 全部来自代码常量/环境变量，不构成攻击面。

## 硬编码凭据（6 条）—— UI 标签与测试/审计假密码

| 位置 | 实际内容 |
|---|---|
| `frontend/src/pages/Admin.tsx:52,56` | 字段中文名标签 `'LLM API Key'` / `'Embedding API Key'`，无任何密钥值 |
| `backend/scripts/audit_auth.py:252,255` | 审计脚本为临时账号轮换用的假密码（`TEST_PW`、`"audit-pw-new45"`） |
| `backend/tests/test_users_auth.py:264,272` | pytest 用例的假密码，作用于测试库/内存库 |

真实凭据一律经 `backend/.env`（已 gitignore，不入仓库）或管理面板
`system_setting` 注入；脚本类真实凭据已走环境变量
（`AUDIT_EMAIL`/`AUDIT_PASSWORD`/`ADMIN_TOKEN`，见 `audit_auth.py` 头部说明）。

## 运行时服务的 SSRF（2 条）——已有纵深防护，跨文件告警

| 位置 | 实际情况 |
|---|---|
| `backend/app/services/alert_service.py:152` | 告警 webhook 推送：URL 仅 admin 可配（require_admin），写入时经 `assert_public_http_url` 公网校验（`config_service.py:140-151`），内网豁免需显式开 `ALLOW_PRIVATE_AI_URLS`；POST 响应不回显（盲 SSRF） |
| `backend/app/services/report_push.py:218` | 日报 webhook 推送：同上口径 |

与脚本误报性质不同：这是「防护存在于写入侧（config_service），扫描器在
读取侧（httpx.post）单文件视角看不见」。维持现状，不改代码。

## 相关的真实风险项（非误报，另行跟踪）

- `backend/.env` 的 `ADMIN_TOKEN` 占位值 `please-change-me`——已于 2026-09-14
  轮换为 `secrets.token_urlsafe(32)` 强随机值。
- `ENVIRONMENT=development` + `COOKIE_SECURE=false`：公网 HTTPS 部署时需改为
  `production`/`true`（启动自检强制），内网 HTTP 部署维持现状。
