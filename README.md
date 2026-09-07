<div align="center">

# 📡 华军科技资讯雷达

**多源科技资讯聚合 · AI 摘要 · 热点事件聚类 · 热度排行 · 每日科技日报**

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-异步任务-37814A?logo=celery&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)
![Docker Compose](https://img.shields.io/badge/Docker%20Compose-部署-2496ED?logo=docker&logoColor=white)

</div>

---

## 项目简介

华军科技资讯雷达是一套面向内部编辑、运营、产品团队的**科技资讯情报系统**：从 IT之家体系频道页面自动采集资讯，经 AI 摘要压缩阅读成本，通过 Embedding 向量聚类把多家媒体的报道归并为同一热点事件，再以热度算法排行、每日自动生成科技日报。

- **V1 解决**「今天发生了什么？」—— 频道热榜、最新资讯时间轴
- **V2 解决**「这件事是什么？为什么重要？有哪些媒体关注？后续如何发展？」—— AI 摘要、热点事件聚合、多来源关联、科技日报

第一阶段基于 IT之家体系频道（软件之家 / Win11之家 / Win10之家 / 智能时代）建设，架构上预留多来源扩展能力。

## ✨ 功能特性

| 模块 | 说明 |
| --- | --- |
| 🔥 **热榜中心** | `HotScore = Engagement × TimeDecay`，纯算法热度、无人工权重；互动数据缺失时自动降级为时间衰减主导 |
| 📰 **最新资讯** | 按发布时间倒序的实时资讯流，支持频道筛选与关键词检索 |
| 🤖 **AI 摘要** | 一句话摘要 + 3~5 条核心信息 + 标签提取；支持任意 **OpenAI 兼容 LLM**，未配置时自动降级为内置 TextRank 抽取式摘要，双模式对上层透明 |
| 🧩 **热点事件聚合** | Embedding 余弦相似度 > 0.85 + 标题关键词（jieba）+ 48h 时间窗，多来源报道归并为一个事件，附事件时间线与相关报道 |
| 📅 **科技日报** | 每日 08:00 自动生成**昨日**日报：TOP 热点、事件摘要、行业变化、重点来源 |
| 📊 **AI 用量统计** | LLM / Embedding 的 token 消耗与调用记录 |
| 🛠️ **运维监控面板** | 基础设施连通性（DB/Redis/Celery）、任务执行记录、**运行时配置热更新**（DB 覆盖 > .env，保存即生效无需重建）、LLM/Embedding 连通性测试 |
| 🌗 **界面主题** | 浅色 / 深色 / 跟随系统三态切换（顶栏与移动端抽屉均可切换）；浅色为 **macOS 设计语言**（Apple 灰窗体 + 白卡悬浮 + 蓝色 accent + 毛玻璃顶栏）；偏好本地持久化、刷新不丢失，「跟随系统」实时响应操作系统深浅色变化 |
| 🔑 **登录与多租户权限** | 全站强制登录（**邮箱 + 密码**，无注册，账号由管理员后台创建）；多租户三级 RBAC（平台超管 / 租户管理员 / 成员）；服务端会话（HttpOnly Cookie，即时吊销）+ 登录失败锁定 + 首登强制改密；完整审计日志（登录/管理操作留痕，超管可查，自动清理） |

## 🏗️ 系统架构

```mermaid
flowchart LR
    SRC[四频道页面<br/>win11/win10/soft.ithome.com · tags/AI] --> CR[crawler 常驻采集<br/>间隔可配/面板热调]
    CR --> PG[(PostgreSQL 16<br/>+ pgvector)]
    PG --> RD{{Redis 任务队列}}
    RD --> AI[ai-worker<br/>AI 摘要 / Embedding]
    RD --> WK[worker<br/>事件聚合 / 日报]
    BT[beat 定时调度<br/>事件聚类 / 每日 08:00 日报] --> RD
    AI --> PG
    WK --> PG
    NG[nginx :80<br/>前端静态资源 + API 反代] --> BE[backend FastAPI :8000]
    BE --> PG
    U(浏览器) --> NG
```

| 服务 | 职责 |
| --- | --- |
| `backend` | FastAPI REST API（容器内 8000，宿主机映射 `127.0.0.1:18000`） |
| `crawler` | 常驻循环：频道页面采集（页面即频道真相）→ 入库 → 派发 AI 任务 |
| `worker` | Celery 默认队列：事件聚合 / 日报 |
| `ai-worker` | Celery AI 队列：摘要 / Embedding |
| `beat` | 定时调度：事件聚类、每日 08:00 生成昨日日报 |
| `postgres` | 业务数据 + pgvector 向量检索 |
| `redis` | 缓存 + Celery 消息队列 |
| `nginx` | 前端静态资源 + `/api` 反向代理（唯一公网入口） |

## 🚀 快速开始

**前置要求**：Docker 20+ 与 Docker Compose v2

```bash
git clone https://github.com/xuboboo/news.git
cd news
cp backend/.env.example backend/.env   # 按需修改 ADMIN_TOKEN 等配置
docker compose up -d --build           # 一键上线全套 8 个服务
```

启动后的访问入口：

| 入口 | 地址 |
| --- | --- |
| 前端站点（含 API 反代） | http://localhost |
| 后端 API | http://127.0.0.1:18000 |
| OpenAPI 交互文档 | http://127.0.0.1:18000/docs |
| 运维监控面板 | http://localhost/admin |

## ⚙️ 配置说明

配置位于 `backend/.env`（模板见 `backend/.env.example`），所有键均可在管理面板「运行时配置」中热覆盖（DB 优先于 .env，保存即生效；传空串 = 回落 .env 默认值）。

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 连接串（业务库） |
| `REDIS_URL` | Redis 连接串（缓存/队列） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 可选，OpenAI 兼容协议；配置后 AI 摘要与事件总结走大模型 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 可选，OpenAI 兼容 Embedding 服务 |
| `EMBED_DIM` | 向量维度（内置模式 256；外部服务需与之一致，严格模式下维度不符即硬失败） |
| `SIM_THRESHOLD` / `CLUSTER_WINDOW_HOURS` | 事件聚类阈值（默认 0.85）与时间窗（默认 48h） |
| `CRAWLER_INTERVAL_MINUTES` | 采集间隔，支持面板热调 |
| `ADMIN_TOKEN` | 管理接口应急后门 Token（脚本/回归用）；未设置时后门不可用（503） |
| `ADMIN_NO_AUTH` | 免鉴权开关，**仅限本机/内网**；公网部署必须为 `false` |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | 首启引导：user 表为空时自动创建「默认租户 + 平台超管」；建号后可移除 |
| `SESSION_TTL_HOURS` / `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCK_MINUTES` | 会话有效期（默认 168h）/ 登录失败锁定阈值（5 次）/ 锁定时长（15 分钟），支持面板热调 |
| `COOKIE_SECURE` | 会话 Cookie Secure 标记，HTTPS 部署必须 `true` |
| `AUDIT_RETENTION_DAYS` | 审计日志保留天数（默认 180，beat 每周自动清理） |
| `EMBEDDING_STRICT` | Embedding 失败/维度不符时是否硬失败（false 会静默降级本地哈希向量、污染聚类） |
| `ALLOW_PRIVATE_AI_URLS` | 是否允许 LLM/Embedding 地址指向私网/环回 |
| `CORS_ORIGINS` | 允许跨域的前端来源，勿用 `*` |

## 📡 API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v2/hot/board?channel=` | 热榜中心 |
| GET | `/api/v2/articles/latest?channel=&q=&limit=` | 最新资讯（时间倒序 + 检索） |
| GET | `/api/v2/article/{id}` / `/api/v2/article/{id}/summary` | 文章详情 / **AI 摘要** |
| GET | `/api/v2/events` / `/api/v2/events/{id}` | 热点事件列表 / 事件详情（时间线、相关报道） |
| GET | `/api/v2/report/daily?date=` / `/api/v2/report/dates` | **科技日报** / 可用日期 |
| GET | `/api/v2/stats` · GET `/api/v2/health` | 系统统计（需登录）/ 健康检查（公开） |
| POST | `/api/v2/auth/login` · `/auth/logout` | 登录 / 登出（HttpOnly 会话 Cookie） |
| GET | `/api/v2/auth/me` · GET `/auth/sessions` | 当前用户 / 我的活跃会话 |
| PUT | `/api/v2/auth/me/password` · DELETE `/auth/sessions/{id}` | 修改密码 / 吊销会话 |
| GET/POST | `/api/v2/admin/tenants` · PUT `/admin/tenants/{id}` | 租户管理（超管） |
| GET/POST | `/api/v2/admin/users` · PUT `/admin/users/{id}` · POST `/admin/users/{id}/password-reset` | 用户管理（超管全局 / 租户管理员限本租户 member） |
| GET | `/api/v2/admin/audit-logs` · `/admin/audit-actions` | 审计日志查询（超管） |
| POST | `/api/v2/admin/crawl` · `/admin/events` · `/admin/report` | 手动触发任务（需超管会话或 Token） |
| GET/PUT | `/api/v2/admin/config` | 运行时配置（敏感值脱敏，留空 = 不修改） |
| GET | `/api/v2/admin/status` · `/admin/task-runs` | 基础设施状态 · 任务执行记录 |
| POST | `/api/v2/admin/test/llm` · `/test/embedding` | AI 连通性测试（可先验证未保存的 key 再存） |

## 🗂️ 目录结构

```
news/
├── backend/
│   ├── app/
│   │   ├── api/v2/        # REST 接口（业务 + admin）
│   │   ├── models/        # article / article_summary / event / daily_report / task_run / usage
│   │   ├── services/      # 摘要、向量、事件、日报业务服务
│   │   ├── algorithms/    # hot_score：HotScore = Engagement × TimeDecay
│   │   ├── ai/            # summary / embedding / event_cluster（LLM 与内置算法双模式）
│   │   ├── tasks/         # Celery：summary / embedding / event / report
│   │   ├── crawler/       # IT之家频道页面采集 + 常驻循环
│   │   ├── core/          # 配置、数据库
│   │   └── utils/         # 中文文本处理（jieba）、URL 安全守卫
│   ├── scripts/           # 运维脚本（重嵌入、数据修复、审计等）
│   └── .env.example       # 配置模板
├── frontend/              # React 18 + TypeScript + Vite + Tailwind
│   └── src/pages/         # 热榜 / 最新 / 事件 / 日报 / AI 用量 / 运维监控
└── docker-compose.yml     # 8 服务一键编排
```

## 👤 账号与登录

全站强制登录：未登录访问任何页面会跳转登录页（`/health` 等基础设施探针除外）。账号**无注册入口**，全部由管理员后台创建（用户与租户页 → 新建用户），初始密码一次性线下发放，**首次登录强制修改**。

- **角色**：`super_admin` 平台超管（租户/用户/审计/运维/成本全部可见）→ `tenant_admin` 租户管理员（本租户内管理成员）→ `member` 成员（热榜/最新/事件/日报只读）
- **会话**：HttpOnly Cookie（SameSite=Lax，默认 7 天），改密/停用/登出即时吊销；连续失败 5 次锁定 15 分钟
- **审计**：登录成功/失败/锁定、用户与租户变更、配置修改、手动触发任务全部留痕（操作者/IP/UA/时间），超管在「审计日志」页可查，保留 180 天自动清理
- **忘记超管密码**：`docker compose exec backend python scripts/manage_user.py reset-password <邮箱> <新密码>`

## 🔧 常用运维命令

```bash
docker compose ps                                      # 服务状态
docker compose logs -f crawler worker ai-worker        # 流水线日志
docker compose exec backend python scripts/init_db.py  # 初始化/补建表
docker compose restart crawler                         # 重启即触发一轮采集
docker compose up -d --build backend                   # 改 .env 后重建生效
```

### 验收脚本（需后端已在 18000 运行）

```bash
# 常规 API 回归（数据端点需先登录）
AUDIT_EMAIL=... AUDIT_PASSWORD=... ADMIN_TOKEN=... python backend/scripts/audit_api.py
# 登录/权限/审计专项（权限矩阵、锁定、会话吊销、CSRF）
AUDIT_EMAIL=... AUDIT_PASSWORD=... ADMIN_TOKEN=... python backend/scripts/audit_auth.py
```

## 🔐 安全须知

- **登录与多租户**：全站强制登录；会话令牌服务端存储（库中仅存 SHA-256，即时可吊销）；密码 bcrypt(cost 12)；变更接口要求 JSON + `X-Requested-With`（CSRF 双保险，配合 SameSite=Lax Cookie）。
- 管理接口（`/api/v2/admin/*`）：super_admin 登录会话，或 `X-Admin-Token` 应急后门（脚本/回归用）；`ADMIN_NO_AUTH=true` 仅限本机/内网调试，公网部署必须关闭。
- `backend` 端口仅绑定 `127.0.0.1`，公网入口统一走 nginx(80)；上 HTTPS 时必须 `COOKIE_SECURE=true`。
- 敏感键（`api_key` / `admin_token`）接口侧只做脱敏展示、不回传明文；留空即表示不修改。
- 生产部署请遵循 `.env.example` 末尾的检查单：更换 `POSTGRES_PASSWORD` 强口令、同步 `DATABASE_URL`、更换强随机 `ADMIN_TOKEN`、配置 `BOOTSTRAP_ADMIN_*` 完成引导。

## 🗺️ Roadmap

- 多来源扩展：极客公园、少数派、36氪、爱范儿、InfoQ
- 周报与趋势分析
- 接入互动数据源后 HotScore 恢复 `Engagement × TimeDecay` 全量形态

---

<sub>本项目为内部私有项目，未授权公开使用与分发（All Rights Reserved）。</sub>
