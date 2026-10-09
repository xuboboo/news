# 草稿箱 Redis / Celery 故障恢复验收报告

日期：2026-09-26（UTC+8）  
结论（基线轮次，历史记录）：**基础队列与若干故障场景已实测；当时发现两条故障恢复缺口。** 本报告末尾追加了 2026-09-26 修复后复测记录，后续结论以追加记录为准。

## 隔离与安全

第二轮使用唯一前缀 `draft-queue-check-26b926`，仅创建同前缀的 `--internal` Docker 网络、Redis AOF 专用具名卷、PostgreSQL、prefork worker、HTTP API 与临时测试容器；容器无宿主端口，不挂生产卷。worker/API 只读挂载当前 `backend/app` 和测试 harness 到 `/srv/app`、`/verify`，工作目录 `/tmp/draft-run`，没有挂载 backend 根目录或读取 `.env`。所有容器环境显式为 `ENVIRONMENT=test`，数据库只指向 `draft_queue_test`，Redis hostname 必须与前缀严格对应；LLM 使用 `https://mock.invalid/v1`、假 key `offline`，worker 进程内替换 `llm_client.chat`，Embedding/Webhook 均为空。不启动 beat/crawler，不 mock broker、Celery task 或生成服务。

为避免误连风险，所有 harness 入口在导入 SQLAlchemy/app 前执行 `verify_article_queue_guard.py`：需显式 opt-in `DRAFT_QUEUE_CHECK=1`；校验唯一格式前缀、`ENVIRONMENT=test`、PostgreSQL host/database、Redis host/port、mock LLM 假地址/key 以及外部集成变量为空；任何检查失败立即退出。

## 场景结果

| 场景 | 结论 | 实测与解释 |
|---|---|---|
| `.delay`真实投递、ai路由、prefork worker、Redis result backend | PASS | 第一轮 task `023eb6a5-9a77-46bb-8d6d-f7088aae46c6` 为 Celery `SUCCESS`，业务结果 `ok=true`；worker 实际消费并保存正文、参考来源、声明和 mock token。第二轮 worker 为生产等价 `prefork -c2`，日志确认只订阅 `ai`。 |
| 普通 `default` 队列不被专属AI worker消费 | PASS | 第三次独立隔离验证通过真实 Celery `send_task("auth_cleanup_task")` 发消息，task `406ad455-ad12-415d-86b1-e79a2b2e98a3` 保持 `PENDING`；Redis `LLEN default=1`、`LLEN ai=0`，专属worker只消费ai。证据 `docs/draft-queue-check-c57a12-route.log`。 |
| 旧generation重复投递 | PASS | 第一轮旧代 task `82ddab5b-2847-4995-9291-5cb4db3f3da8` 返回 Celery `SUCCESS`、业务 `ok=false/error='generation superseded'`；mock调用数未增加，ready正文未被覆盖。 |
| advisory lock、生产30秒 self.retry | PASS | 第一轮 task `cc8f9bfd-c00b-4d04-a03a-d2c645f55176` 在真实 PG advisory lock 下记录 `Retry in 30s`、结果 `RETRY`；释放锁后同 task id 再投递，最终 `SUCCESS` / 草稿 ready。未 patch `self.retry`、未缩短 countdown。 |
| 模型失败业务落账与Celery状态差异 | PASS | mock `LLMAPIError` 使草稿 failed 且写 `article/api_fail`；Celery `SUCCESS`、业务结果 `ok=false`，明确为已处理业务失败，不是Celery执行异常。旧测试日志及第一轮报告有细节。 |
| API `_dispatch` 发布失败、broker恢复后新代再生成 | PASS | 第二轮用真实 FastAPI HTTP 路由、seed 的 member/tenant 与会话 Cookie、Mutation header；API进程先执行 `/__queue_check/prime` 建立 publisher 连接。Redis `PAUSE` 后真实 `POST /api/v2/drafts` 触发未 patch 的 `.delay`，profile设置 socket/connect timeout 0.5s、发布重试最多1次；约2.089秒后该代落 `failed`，错误 `Timeout reading from socket`。另一次 stop Redis 确认连接名解析失败也落 `failed`。Redis恢复后真实 `POST /api/v2/drafts/7/regenerate` 入队并 ready，`gen_count=2`，Celery task `3c0b6111-4c6e-4e78-9020-072702e2abc5` 成功。有限超时/重试仅用于本次故障测试，不代表生产发布配置。 |
| prefork子进程在mock模型运行中 SIGKILL | **FAIL / 缺口已复现** | `-c2` 下 child PID 14 正阻塞 mock chat；task `8158c772-3888-40c8-86cd-885953406576`，draft3/gen1。只对专属 worker 子进程发 SIGKILL。Celery 记录 `WorkerLostError: Worker exited prematurely: signal 9`，AsyncResult `FAILURE`；PG草稿仍 `generating/gen_count=1/error=''`；随后 `ai LLEN=0`、`unacked_index ZCARD=0`，未自动重投。配置为 `acks_late=True`、默认 `task_reject_on_worker_lost=False`、visibility 4h。真实API将 `updated_at` 快进16分钟后 POST regenerate，`gen_count=2`，task `f0880335-435a-4991-8769-39fa4148b0f2` 成功并 ready。此处揭示生产默认child-loss并无立即自愈，需依赖stale人工/接口恢复。 |
| worker父容器 SIGKILL与消息恢复机制 | PROFILE PASS（非生产时延） | 独立诊断worker在投递前将 broker、result backend、global `visibility_timeout` 一致降为15秒。task `2244fab4-19f9-4d1b-8de2-64555c049bd3` 在mock内阻塞时对专属worker执行 `docker kill`；Redis观察到 `unacked_index=1`、`ai LLEN=0`，PG仍generating。重启同profile worker后日志收回同task id；mock释放后 task `SUCCESS`、draft6 ready。当前日志显示重新消费与最终成功。15秒是机制诊断profile；生产配置4小时未等待，不能宣称生产4小时恢复时延已端到端计时。 |
| 锁重试次数耗尽 | **FAIL / 缺口已复现（诊断profile）** | worker-only 将 `article_gen_task.max_retries=1`，仍保留真实 broker 重投与30秒 countdown；PG advisory lock持有使 task `8d45f701-7fcf-472e-93ee-5aa9a43e92bc` 首次真实 `Retry in 30s`，再次投递抛 `MaxRetriesExceededError`，Celery `FAILURE`。草稿4仍 `generating/gen_count=1/error=''`，没有 crash-mark。异常在 `tasks.py` 的 `raise self.retry(countdown=30)`、服务异常标记 `try/except` 之外。真实HTTP stale regenerate后gen_count2并ready。该验证不是将生产120次重试全部跑完；展示同一耗尽代码路径。 |
| Redis AOF容器重启 | PASS（单实例持久化范围） | Redis使用专用具名volume、`--appendonly yes --appendfsync everysec`。暂停worker后通过真实API投递draft5，再 `WAIT 1 1000`（单实例无副本，返回0）。消息在检查时已从`ai` list转为unacked reservation（LLEN=0）；重启Redis容器、复用原volume后返回PONG。worker重连记录 `Restoring 1 unacknowledged message(s)`，随后task `44535983-8069-405f-b3c8-b32ab86b7742` 成功，draft5 ready。证明本次AOF恢复路径，不是Redis HA/副本持久性保证。 |
| 15分钟stale真实API恢复、generation fence | PASS（stale API路径） | 对worker lost与retry exhausted的草稿分别只在独立PG中快进`updated_at`到16分钟前（没有等待16分钟），使用真实 member Cookie 请求 `/api/v2/drafts/{id}/regenerate`；均返回gen_count+1，最终ready。第一轮已有旧generation再次投递不覆盖正文的真实测试；本轮未人为制造一个仍运行的旧LLM在新代ready后迟到写入，不能单独声称验证了这种竞态。 |
| 相关后端测试 | PASS | 临时下载 pytest 9.0.2 wheels，只读挂载到测试容器，`--no-index` 安装到临时 `/tmp`；业务镜像不变。命令覆盖 `test_article_draft.py`、`test_article_draft_generation_fence.py`、`test_article_draft_pg.py`、`test_usage_api.py`：**60 passed，2条 Starlette/httpx 弃用警告**。其中PG专用模块5项全部执行通过。`TEST_DATABASE_URL` 指向独立 `draft_queue_test`；`DATABASE_URL` 只用于防误连比较，设置成不同且不可解析的测试名字，未连接。 |

## 已确认缺口与建议

1. **SIGKILL prefork child会导致任务 FAILURE、Redis未确认记录消失，草稿仍 generating。** 当前 `task_reject_on_worker_lost=False` 与实测相符。stale API可以自救，但不是即时自动重投。建议评估专用文章任务开启 `task_reject_on_worker_lost=True` 的风险（可能造成毒任务循环），并确保任务幂等、提供监控告警及生成中stale再生成入口。
2. **锁重试耗尽不会把草稿标成failed。** `self.retry` 位于异常落库保护外；耗尽异常令Celery任务FAILURE，业务草稿留在generating。建议在耗尽路径显式标记当前代次失败或增加可靠的stale repair机制。没有改业务代码来掩盖该结果。
3. **生产visibility为4小时。** 15秒profile只证明Redis unacked重投机制，并不把4小时边界缩短为已验证的生产时延。
4. `_dispatch`测试连接profile的超时与重试数是显式差异；HTTP接口失败状态、数据库落failed和恢复后重生成都是真实路由及broker路径。

## 复查证据与复现 harness

核心脚本：`backend/scripts/verify_article_queue.py`、`verify_article_queue_worker.py`、`verify_article_queue_guard.py`、`verify_article_queue_api_server.py`、`verify_article_queue_api_client.py`。  
证据文件：

- `docs/draft-queue-check-26b926-worker.log`：第二轮prefork/visibility恢复worker日志。
- `docs/draft-queue-check-26b926-api.log`：API真实HTTP请求日志。
- `docs/draft-queue-check-26b926-redis.log`：Redis AOF生命周期日志。
- `docs/draft-queue-check-26b926-database-summary.json`：清理前草稿最终态、gen_count及token用量快照。
- `docs/draft-queue-check-26b926-fault-evidence.log`：第一轮及已退出worker容器的SIGKILL、重试耗尽、AOF恢复、测试命令和实测摘要。之前轮次日志因替换诊断容器而不再能从Docker取回，此文件保留了当时终端原始事件摘录和关键查询结果。
- `docs/draft-queue-check-c57a12-route.log`：普通default队列任务保持待处理、`ai`队列为0的路由证据。
- `docs/草稿箱Redis与Celery隔离验收报告.md`：首轮基础场景结果。

## 清理与最终判断

测试容器在报告保存后按 `draft-queue-check-26b926-*` 精确清理，包括 worker/API/PG/Redis/lockholder，独立internal网络和Redis具名volume；其余业务实例 `hj-tech-radar-*` 未停止、重启、杀死或触碰。除 harness、测试辅助入口、验收报告与证据文件外，没有修改业务实现或生产配置。

**最终判断（仅首轮基线）：基础队列、HTTP发布故障处理、AOF重启路径和相关回归测试通过；worker child丢失与retry耗尽当时暴露“Celery FAILURE但草稿遗留generating”的恢复缺口。修复及复测结果见 [草稿队列故障恢复修复后隔离复测报告](draft-queue-check-9d26fb-fix-report.md)，其中记录了新的故障注入证据、owner保护及剩余边界。**