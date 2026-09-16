# 华军科技资讯雷达 V2.0 — 后端系统

严格依据《华军科技资讯雷达 V2.0 产品 PRD》与《技术开发 PRD》实现的完整后端，
已在生产环境（Docker）运行并通过全链路测试。

## 技术栈（PRD 第 2/3/13 节）

| 层 | 组件 |
| --- | --- |
| API | FastAPI + Uvicorn |
| 数据库 | PostgreSQL 16 + **pgvector**（向量检索） |
| 缓存/队列 | Redis 7 |
| 异步任务 | Celery（default 队列 + ai 队列 + beat 定时） |
| 采集 | 常驻爬虫服务（IT之家体系四频道页面） |
| AI | LLM 摘要服务（OpenAI 兼容协议）+ Embedding 服务 + 事件聚类 |
| 部署 | Docker Compose：postgres / redis / backend / worker / ai-worker / beat / crawler / nginx(frontend) |

## 快速启动

```bash
cd d:\桌面迁移\code\news
docker compose up -d --build     # 一键上线全套服务
```

服务地址：
- 前端（含 API 反代）：http://localhost:80
- 后端 API：http://localhost:18000 （容器内 8000，宿主机 8000 被 V1.1 服务占用故映射 18000）
- OpenAPI 文档：http://localhost:18000/docs

## 数据流水线（PRD 第 2/5/8/11 节）

```
频道页面（页面即频道真相）→ crawler(每30分钟) → article 表
    → summary_task（AI 摘要 → article_summary 表）
    → embedding_task（向量 → article.embedding, pgvector）
    → event_task（每15分钟：相似度>0.85 + 标题关键词 + 48h 时间窗 → event/event_article 表）
    → report_task（每日 08:00：daily_report 表）
```

四频道 = 四个采集页面（只收页面内文章，不做关键词归类，页面外文章不入库）：

| 频道 | 采集页面 |
| --- | --- |
| Win11之家 | win11.ithome.com（/tags/Win11/ 兜底页已 404，弃用） |
| Win10之家 | win10.ithome.com（/tags/Win10/ 兜底） |
| 软件之家 | soft.ithome.com（/tags/软件/ 已 404） |
| 智能时代 | next.ithome.com（官方「智能时代」子站，2026-09 由 /tags/AI/ 标签页修正而来，不留标签页兜底以保口径纯粹） |

## 目录结构（PRD 第 4 节）

```
backend/app/
├── api/v2/          # REST 接口
├── models/          # article / article_summary / event / event_article / daily_report
├── services/        # 摘要、向量、事件、早报四个业务服务
├── algorithms/      # hot_score（HotScore = Engagement × TimeDecay）
├── ai/              # summary.py / embedding.py / event_cluster.py
├── tasks/           # Celery：summary_task / embedding_task / event_task / report_task
├── crawler/         # IT之家频道页面采集 + 常驻循环
├── core/            # 配置、数据库
└── utils/           # 中文文本处理（jieba）
```

## API 一览（PRD 第 12 节 + V1 能力）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/v2/hot/board?channel= | 热榜中心 |
| GET | /api/v2/articles/latest?channel=&q=&limit= | 最新资讯（时间倒序） |
| GET | /api/v2/article/{id} | 文章详情 |
| GET | /api/v2/article/{id}/summary | **AI 摘要**（PRD 12） |
| GET | /api/v2/events | **热点事件**（PRD 12） |
| GET | /api/v2/events/{id} | 事件详情（含时间线/相关报道） |
| GET | /api/v2/report/daily?date= | **今日早报**（原软件日报，PRD 12） |
| GET | /api/v2/report/dates | 可用早报日期 |
| GET | /api/v2/stats / health | 系统统计 / 健康检查 |
| POST | /api/v2/admin/crawl · /admin/events · /admin/report?date= | 手动触发任务（需 X-Admin-Token） |

## 运维管理面板

前端新增「运维监控」页（`/admin`），对应后端 `app/api/v2/admin.py`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/v2/admin/status | 基础设施连通性（DB/Redis/Celery worker）、AI 配置就绪状态、数据水位、最近失败任务 |
| GET / PUT | /api/v2/admin/config | 运行时配置：DB 覆盖优先于 .env，保存立即生效，无需重建镜像；敏感值脱敏展示；传空串 = 回落 .env 默认 |
| POST | /api/v2/admin/test/llm · /test/embedding | 连通性测试（可先用未保存的 key/base_url/model 验证再保存；embedding 会校验向量维度与 EMBED_DIM 是否一致） |
| GET | /api/v2/admin/task-runs | 任务执行记录（crawler / event / report 的成败、耗时、错误信息） |
| POST | /api/v2/admin/crawl · /events · /report | 手动触发任务（原有接口，现需 Token） |

**鉴权**：所有上述接口要求请求头 `X-Admin-Token` 与生效的 `admin_token` 一致。
Token 来源优先级同样是 DB 覆盖 > `.env` 的 `ADMIN_TOKEN`；**未配置时管理面板不可用**（接口返回 503 并给出设置提示）。
注意：修改 admin_token 会立即使旧 Token 失效。

**任务执行记录**：`task_run` 表记录采集（常驻爬虫每轮 + 手动触发）、事件聚合、早报任务的
started/finished/耗时/成败/错误摘要；逐篇 summary/embedding 任务不记录（量大且低价值）。

## AI 能力说明（重要）

`ai/summary.py` 与 `ai/embedding.py` 均为**两级实现**：

1. **LLM 模式**：在 `backend/.env` 配置任意 OpenAI 兼容服务后自动启用：
   ```
   LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
   LLM_API_KEY=sk-xxx
   LLM_MODEL=glm-4-flash
   ```
   Prompt 按 PRD 第 7 节要求（客观、不增加观点、保留事实）输出
   `{"summary","points","tags"}`；事件总结与核心影响同样由 LLM 交叉聚合生成。
2. **内置算法模式**（当前运行状态，未配置 LLM 时自动生效）：
   - 摘要：`extractive-textrank-v2`——真 TextRank（句子相似图 PageRank）× 标题相关
     覆盖率 × 导语位置权重，MMR 去冗余选 5 句按原文顺序输出
   - 向量：`local-hash-256-v2`——字符级 n-gram 符号哈希（±1 抵消碰撞偏置）+
     sublinear TF，256 维 L2 归一，确定性可离线
   - 聚类：Embedding 余弦 > sim_threshold + 标题关键词 Jaccard + 48h 时间窗
     （PRD 第 10 节）；sim_threshold 支持管理面板热调（DB 覆盖 .env，聚类实时读取）

两模式对上层完全透明，配置 LLM 后无需改任何代码。

### 本地算法 v1 → v2 迁移 runbook

v2 相对 v1 的量程变化：符号哈希消除了碰撞导致的相似度虚高，无关文本对从
0.3-0.5 回落到 0-0.2，同事件对亦整体下移——**0.85 的旧阈值大概率偏严**。
未配置 Embedding API（本地哈希即主链路）时，代码升级后必须执行：

```bash
docker compose up -d --build backend worker ai-worker beat crawler   # 1. 新代码生效（5 个 Python 服务共用同一镜像）
docker compose exec backend python scripts/repair_data.py   # 2. 全量重写摘要(v2)+重嵌向量(v2)+重聚类+重建日报
docker compose exec backend python scripts/sim_report.py    # 3. 看相似度分布与建议阈值
# 4. 按报告在管理面板热调 sim_threshold（如 0.85 → 0.75），再跑一遍 repair_data.py 重聚类
```

`sim_report.py` 以现有事件成员对为正例、随机文章对为负例输出分位数分布与
建议起点。⚠️ 实测（2026-09）：同站新闻底噪高（随机对 p50≈0.78），0.80 会
过合并（无关文章混入事件），**保持 0.85 精确率优先**；若调低，务必人工抽查
多成员事件的一致性——误合并需 TRUNCATE `event`/`event_article` 后重跑
repair_data.py 才能拆散（增量聚类只合并不拆分）。前台抽查热榜/最新页摘要与事件聚合质量收尾。

## 数据真实性说明

- 文章、标题、正文、时间均**真实采集**自 IT之家体系频道页面（见上表四频道映射；
  2026-09 起废弃主站 RSS 全量抓取 + 关键词归类，频道归属以页面为准）。
- IT之家公开的阅读/评论数接口已下线（实测 404），`views/comments/shares` 字段保留为 0，
  热度算法自动降级为时间衰减主导；未来接入任何互动数据源后，HotScore 公式自动恢复
  `Engagement × TimeDecay` 全量形态，无需改代码。
- 运维触发接口（/admin/*）与 Celery beat 定时任务均已在容器中验证可用。

## 常用运维命令

```bash
docker compose ps                       # 服务状态
docker compose logs -f crawler worker ai-worker   # 流水线日志
docker compose exec backend python scripts/init_db.py   # 初始化/补建表
docker compose restart crawler          # 立即触发一轮采集（启动即采集）
```
