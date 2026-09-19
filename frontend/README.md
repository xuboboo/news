# 华军科技资讯雷达 V2.0 — 前端系统

依据《华军科技资讯雷达 V2.0 产品 PRD》与《技术开发 PRD》实现的完整前端系统（生产构建版本）。

## 访问方式

```bash
cd frontend
npm install        # 首次
npm run build      # 构建生产包
npm start          # 本机预览（默认 http://localhost:8080）
```

也可以自定义端口：`node server.mjs 9000`。默认仅监听 `127.0.0.1`（本机预览）；确需局域网访问时显式 `HOST=0.0.0.0 node server.mjs`。

> 生产部署不走 server.mjs：正式入口是根目录 Docker Compose 的 nginx 容器
> （`frontend/Dockerfile` 多阶段构建，dist 内置于镜像，含 API 反代与安全头）。

## 功能模块（与 PRD 对应）

| 模块 | 路由 | PRD 依据 |
| --- | --- | --- |
| 热榜中心 | `/#/hot` | 3.1 热榜中心，HotScore = Engagement × TimeDecay，无人工权重 |
| 最新资讯 | `/#/latest` | 3.2 最新资讯，publish_time 倒序，标题/来源/时间/AI摘要/标签 |
| 热点事件 | `/#/events` | 5/6 热点事件聚合（Embedding 相似度 > 0.85 + 标题关键词 + 实体 + 时间窗） |
| 事件详情 | `/#/events/:id` | 6 热点事件页面：标题/热度/AI总结/核心影响/相关报道/时间线 |
| 今日早报 | `/#/report` | 7 日报（现「今日早报」）：今日头条/热点事件/事件摘要/行业变化，支持多日期 |

文章详情弹窗对应 PRD 第 4 节 AI 摘要输出：一句话摘要 + 核心信息（3-5条）+ 标签，并展示关联热点事件。

## 目录结构

```
frontend/
├── src/
│   ├── api/
│   │   ├── client.ts    # 后端 /api/v2 接口封装（fetch，带超时与不可达降级）
│   │   ├── auth.ts      # 登录/会话接口
│   │   └── admin.ts     # 管理面板接口
│   ├── components/      # 布局、通用组件、文章弹窗
│   ├── pages/           # 五个页面
│   ├── types.ts
│   └── utils/format.ts
├── server.mjs           # 生产静态服务器（零依赖，Node 原生 http）
└── dist/                # 生产构建产物
```

## 与后端对接

`src/api/client.ts` 的每个函数与后端 `/api/v2` 接口一一对应（文件头有注释映射表），
已全部为 fetch 直连实现（带超时、错误处理；后端不可达时空态降级并自动重连）。
全站强制登录（HttpOnly 会话 Cookie），401 自动跳转登录页。

- `GET /api/v2/hot/board?channel=`      → 热榜
- `GET /api/v2/articles/latest`          → 最新资讯
- `GET /api/v2/events` / `events/{id}`   → 热点事件
- `GET /api/v2/article/{id}/summary`     → AI 摘要
- `GET /api/v2/report/daily?date=`       → 今日早报

## 说明

- 页面数据均为后端真实采集，无内置演示数据；后端不可达时显示空态并按间隔自动重连。
- 生产部署：根目录 Docker Compose 的 nginx 容器（`frontend/Dockerfile` 多阶段构建产物）；`server.mjs` 仅本机预览/应急使用。路由使用 hash 模式，无需服务端重写规则。
