# 华军科技资讯雷达 V2.0 — 前端系统

依据《华军科技资讯雷达 V2.0 产品 PRD》与《技术开发 PRD》实现的完整前端系统（生产构建版本）。

## 访问方式

```bash
cd frontend
npm install        # 首次
npm run build      # 构建生产包
npm start          # 上线（默认 http://localhost:8080）
```

也可以自定义端口：`node server.mjs 9000`。默认仅监听 `127.0.0.1`（本机预览）；确需局域网访问时显式 `HOST=0.0.0.0 node server.mjs`。

## 功能模块（与 PRD 对应）

| 模块 | 路由 | PRD 依据 |
| --- | --- | --- |
| 热榜中心 | `/#/hot` | 3.1 热榜中心，HotScore = Engagement × TimeDecay，无人工权重 |
| 最新资讯 | `/#/latest` | 3.2 最新资讯，publish_time 倒序，标题/来源/时间/AI摘要/标签 |
| 热点事件 | `/#/events` | 5/6 热点事件聚合（Embedding 相似度 > 0.85 + 标题关键词 + 实体 + 时间窗） |
| 事件详情 | `/#/events/:id` | 6 热点事件页面：标题/热度/AI总结/核心影响/相关报道/时间线 |
| 今日早报 | `/#/report` | 7 日报（现「今日早报」）：今日头条/热点事件/事件摘要/行业变化/重点来源，支持多日期 |

文章详情弹窗对应 PRD 第 4 节 AI 摘要输出：一句话摘要 + 核心信息（3-5条）+ 标签，并展示关联热点事件。

## 目录结构

```
frontend/
├── src/
│   ├── api/
│   │   ├── data.ts      # 文章库（38 篇）+ 事件库（8 个聚合事件）
│   │   └── client.ts    # API 层：热度算法 / 事件聚合 / 早报生成
│   ├── components/      # 布局、通用组件、文章弹窗
│   ├── pages/           # 五个页面
│   ├── types.ts
│   └── utils/format.ts
├── server.mjs           # 生产静态服务器（零依赖，Node 原生 http）
└── dist/                # 生产构建产物
```

## 与后端对接

`src/api/client.ts` 的每个函数与后端 `/api/v2` 接口一一对应（文件头有注释映射表）。
当前由本地数据引擎按后端契约返回数据；后端（FastAPI + pgvector + Celery）就绪后，
只需将函数实现替换为 fetch 调用，页面代码零改动。

- `GET /api/v2/hot/board?channel=`      → 热榜
- `GET /api/v2/articles/latest`          → 最新资讯
- `GET /api/v2/events` / `events/{id}`   → 热点事件
- `GET /api/v2/article/{id}/summary`     → AI 摘要
- `GET /api/v2/report/daily?date=`       → 今日早报

## 说明

- 演示数据的时间戳基于当前时间动态生成（分钟级偏移），页面始终呈现"实时"效果。
- 部署：将 `dist/` 放到任意静态服务器（Nginx / server.mjs）即可；路由使用 hash 模式，无需服务端重写规则。
