# 草稿队列故障恢复修复后隔离复测报告

日期：2026-09-26（UTC+8）

## 结论

**代码修复及隔离故障注入通过；建议在生产变更前部署并观察，不启用全局 worker-lost 自动重投。** Prefork 子进程 `SIGKILL` 后，Celery原任务仍为 `FAILURE`，但专属失败信号在父进程用独立数据库短会话将本代草稿标记 `failed`；真实HTTP regenerate可立即开启新代。锁重试耗尽后先验证同稿活动锁：锁空闲则failed；活动owner存在则保持generating，安排一次超过一小时hard limit的延时确认，不能抢断合法生成。生产恢复承诺仍有明确边界，见下文。

## 安全隔离

本轮使用唯一前缀 `draft-queue-check-9d26fb-fix`，仅创建同前缀 `--internal` Docker网络、临时PostgreSQL/pgvector数据库 `draft_queue_test`、Redis、prefork `-c2` worker、API和临时客户端容器。应用与 `backend/scripts` 只读挂载到测试容器，没有挂生产卷或读取真实 `.env`。LLM配置为 `https://mock.invalid/v1` 和假key `offline`，worker内mock调用，不调用外部AI。无Embedding、Webhook、beat、crawler。API只将宿主端口绑定 `127.0.0.1:18089`。本轮没有停止、重启、杀死或连接任何 `hj-tech-radar-*`业务容器、生产数据库或生产Redis。

## 实现与安全取舍

- 不全局打开 `task_reject_on_worker_lost`，保持默认False；避免毒任务SIGKILL后无限即时重投。仅 `article_gen_task` 且仅 `WorkerLostError` 使用Celery `task_failure`信号处理；要求调用含显式generation。其他任务、其他异常不改变。
- 父进程信号处理用独立短DB会话；写失败前尝试与生成服务一致的PostgreSQL事务advisory lock，再以 `draft id + status=generating + gen_count=generation` CAS更新并提交。新一代、ready、已删除记录不会被修改。非预期任务异常兜底也改用同一锁及代次保护，避免异常的重复任务将正在合法生成的草稿标为失败；没有显式代次的旧任务不会猜测并改写状态。处理异常记录安全日志但不抛回worker。
- 同稿锁重试耗尽捕获 `MaxRetriesExceededError`。若能取得advisory lock，立即把匹配代次标failed并使原任务保持Celery FAILURE；不再循环重派。若锁仍被活动owner持有，不改草稿并安排一次3605秒后确认任务；确认再次获取同一锁并CAS。新确认任务显式路由到`ai`队列。隔离worker日志显示两条自动安排的确认消息（`6e1c9e32-0fec-4533-8c60-16b8e18957bf`、`7cf7bf08-1fea-4afc-b43b-a29f386a39fe`）已被只消费`ai`队列的worker接收；这只能证明入队和路由，**不是**3605秒到期执行验收。
- 有界确认超过现有1小时硬任务时限5秒。它不会抢占活动owner；owner成功先置ready则确认CAS无效；owner丢失后则失败标记。确认任务未能排队或执行、父进程/数据库同时故障时不承诺即时恢复，stale人工/API入口仍保留。

## 实际注入与结果

| 场景 | 实测 |
|---|---|
| Child SIGKILL | Prefork child PID 14在mock模型调用阻塞时被SIGKILL。task `a7b63033-00f2-46f5-bfe5-d498fa1380a3` 返回Celery `FAILURE`，父进程记录 `WorkerLostError: Worker exited prematurely: signal 9`；Redis `unacked_index=0`，未循环重投；draft 2/gen1立即为 `failed`，错误记录包含task id。真实HTTP `POST /api/v2/drafts/2/regenerate` 无需等待15分钟，gen2成功ready；task `6e3911e4-ea94-4666-9218-812657549ee2`。 |
| 锁重试耗尽 | worker诊断profile只将 `article_gen_task.max_retries` 设为1，保留真实Redis、PG advisory lock和真实30秒countdown。task `1dc0e1b3-52b5-4a66-a31f-6a2d446504b2`先真实记录 `Retry in 30s`，后抛 `MaxRetriesExceededError`、Celery `FAILURE`。独立锁holder仍持锁时草稿留在generating，无抢占；自动延时消息已被`ai` worker接收。锁释放后，测试**提前单独投递同一确认任务** `e4333bd9-48e0-480f-95c0-9cdfc30220ea`，它将gen1置failed。真实HTTP regenerate使draft3/gen2 ready，task `bfc2957d-9ebd-4b19-a1d3-1ced831349f3`。 |
| 同稿两个合法worker竞争 | owner task `c48f7b95-9b4a-4e87-8fba-84c72052a9e7`持有同稿PG锁并阻塞于mock LLM；重复task `7b3a419e-980f-4cea-8754-9ad5c5e11b37`真实等待30秒后重试耗尽。活动owner尚未释放锁时状态仍为generating，自动延时确认已入队但没有把owner的草稿标失败。放行owner后gen1成功ready；随后**提前单独投递**确认任务 `e4188743-f200-4749-92b6-30f3b4a73161`，它仍保留ready正文。真实HTTP推进到gen2 ready；旧gen1重复投递 `9960c361-4430-4fdd-b6ec-1c924cb4fff5`不修改新稿。 |
| 非目标信号及CAS | 单测验证只处理指定任务+WorkerLostError+显式generation；数据库恢复异常不会穿透信号回调。PostgreSQL测试验证活锁阻止标记、锁释放后可标失败、新代次不受旧代影响。 |

`max_retries=1`是为了有限时间注入耗尽异常的诊断条件，不是生产变更；生产定义仍为120，最多约一小时等待。Redis生产visibility仍为4小时，没有把缩短诊断profile宣称成生产恢复时延。真实HTTP regenerate后的正常生成调用是worker内 mock LLM。

## 测试与证据

- 离线相关后端测试：`test_article_draft_recovery.py`、`test_article_draft.py`、`test_article_draft_generation_fence.py`、`test_usage_api.py`：**63 passed**（2条既有Starlette/httpx弃用警告）；新增断言覆盖非预期异常的安全恢复、3605秒countdown、`ai`路由和全局拒收重投开关关闭。
- PostgreSQL专用隔离模块：**6 passed**，新增活动owner锁与CAS测试；`TEST_DATABASE_URL`只指独立测试数据库。
- 前端Vitest：**7个测试文件、46 passed**（仅React Router未来版本提示）。
- Prefork worker实际日志：`docs/draft-queue-check-9d26fb-fix-worker.log`；SIGKILL与状态证据：`docs/draft-queue-check-9d26fb-fix-fault-evidence.log`。
- 草稿/用量测试库快照：`docs/draft-queue-check-9d26fb-fix-database-summary.json`。

## 剩余风险与上线要求

硬时限导致的异常可能不是 `WorkerLostError`，此专用处理器不会捕获。worker父进程与数据库同时不可用、确认任务无法入队/执行，也不能保证立即落终态；此时保留现有generating超过15分钟后的stale API/人工重新生成入口。未修改全局其他worker任务或Redis 4小时visibility配置。

上线前须保证 `backend` API与 `ai-worker` 同版本并重建；确认全局 `task_reject_on_worker_lost`仍关闭；部署后核对Celery任务失败与草稿状态、观察确认任务路由到ai队列及stale入口可用。此结论是故障恢复代码的隔离验收，不是生产Redis 4小时恢复时延或外部AI端到端验收。

## 清理

本轮精确移除 `draft-queue-check-9d26fb-fix-*` 容器与独立网络，无持久卷；`hj-tech-radar-*`业务实例保持运行。
