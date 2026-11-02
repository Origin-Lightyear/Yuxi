# 待办收件箱接入企业智能工作台

状态：implemented
类型：feature
Owner：backend/package/yuxi/repositories/work_item_repository.py

## 问题

数字员工、定时任务和审批中断都会产生一次线程执行，但用户没有一个跨线程的入口查看「我发出去的事现在到哪了」。现有 `AgentRun` 只能在单个会话里看，管理员任务中心是 Durable Task，不属于同一个领域；如果再建一张 `todo_items` 表由 worker 同步，就会产生第二套任务事实和一致性风险。

本提案实现设计文档中的 P2「待办事项」：把当前用户每条线程的最新顶层 Run 投影成统一收件箱，提供筛选、已读、详情和审批跳转。P2 不实现员工互相派活、截止时间、通知和 P3 转交单，但在 DTO、key 和前端动作中预留 `handoff` 来源。

设计文档：`docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

## 决策

### 实现方案

`AgentRun` 继续拥有执行事实。新增 `backend/package/yuxi/repositories/work_item_repository.py`，用 `row_number() OVER (PARTITION BY conversation_thread_id ORDER BY created_at DESC, id DESC)` 取每线程最新一条顶层 Run；只接受 `run_type IN ('chat','resume')`，排除 `source IN ('agent_call','agent_evaluation')`，因此 subagent Run 不进入待办。

状态映射在 SQL 中完成：`pending -> queued`；`running|cancel_requested -> running`；`interrupted + human_approval_required|ask_user_question_required -> waiting_approval`；其他 `interrupted -> failed`；终态 `completed|failed|cancelled` 原样透传。`queued` 只来自已经创建、状态为 `pending` 的 Run；`AgentRunRequest.status='queued'` 仍只是等待派发的请求，不单列成待办。

新增 `work_item_views(id, uid, item_key, viewed_at)`，`(uid,item_key)` 唯一。key 固定 `run:<run_id>`，已读粒度是 Run 而不是线程；PostgreSQL 用 `ON CONFLICT` 原子 upsert，重复标记幂等。可见范围只按 `AgentRun.uid=当前用户`，不因 Agent 后续停用或取消共享而隐藏本人历史 Run。

`backend/package/yuxi/services/workbench_service.py` 拥有 key 解析、DTO、标题回退、发起方、审批摘要和未读语义；`backend/server/routers/workbench_router.py` 只做薄路由，提供 `GET /api/workbench/todos`、`GET /api/workbench/todos/{key}`、`POST /api/workbench/todos/{key}/viewed`、`GET /api/workbench/todos/summary`。`source=scheduled_agent` 的 `initiator_type` 为 `digital_employee`，名称取 Agent；其他 Run 的发起方是当前用户。

前端新增 `/workbench` 页面、导航未读徽标、状态筛选、汇总卡、列表和详情抽屉。详情只提供「去对话处理」跳转到现有 `AgentCompWithThreadId`，审批参数和执行上下文仍由会话中的 `HumanApprovalModal` 处理；P2 不在待办页复制审批 UI。

## 替代方案

- **新建 `todo_items` 表由 worker 同步**：查询更简单，但引入第二事实源、双写和失败补偿；P2 不需要任务指派和截止时间，收益不足。
- **把 `AgentRunRequest` 也投影成待办**：用户会看到没有 Run 的排队请求，且请求派发后还要合并成同一条；当前 `queued` 只表示已创建但尚未执行的 Run，语义更稳定。
- **按线程已读**：同一线程后续新 Run 会被误判已读；按 `item_key` 才能让新执行重新出现未读。
- **复用管理员任务中心**：Durable Task 面向管理员运维，不是用户的 AI 执行收件箱。
- **在待办页重建审批组件**：会复制 approval payload 的加载、版本和 resume 语义；跳回会话是唯一已有事实 Owner。

## 后果

- 待办是只读投影，不拥有执行状态；Run 更新后页面下一次查询自然反映，无需 worker 同步。
- 同一线程出现新 Run 后，旧 Run 的详情 key 返回 404，避免展示过期审批；用户从列表总能看到最新事实。
- 审批详情仍需回到会话处理；待办页只显示「等待工具审批」或「等待回答执行问题」，不承诺展示原始参数。
- 已读记录会随投影 key 保留；Run 被清理后留下孤立已读行，当前规模可接受，后续可随 Run 清理一并回收。
- 未读目前只在汇总卡展示，列表接口没有 `unread` 筛选；避免客户端过滤破坏分页总数。需要时另行提案服务端筛选。
- `handoff` 已进入 key 解析、DTO 分支和前端动作，但 P2 不创建该类记录；P3 接入时必须扩展 repository 的双源投影和可见范围。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 每线程只出现最新顶层 Run，旧 Run 详情 404 | 同线程旧 Run 重复出现或仍可打开 | `WorkItemRepository._latest_subquery`、`get_latest_run` | `pytest test/unit/repositories/test_work_item_repository.py`：3 passed；真实 HTTP 旧 key 404 | 去掉 `rn=1` 或 `run_id` 条件后测试变红 | Passed |
| subagent、agent_call、agent_evaluation 不进入收件箱 | 子任务或评测调用被当用户待办 | `TOP_LEVEL_RUN_TYPES`、`EXCLUDED_SOURCES` | 同 repository 测试构造 subagent/agent_call 行，结果只返回两条顶层 Run | 删除排除条件后测试变红 | Passed |
| `pending -> queued`，审批中断 -> `waiting_approval`，普通中断 -> `failed` | 执行中/待审批/失败错位 | `WorkItemRepository._status_expression`、`workbench_service._approval_kind` | repository 状态聚合测试；service DTO 测试覆盖 tool/question；真实 HTTP `status=waiting_approval` 只返回审批 Run | 把普通 interrupted 映射成 waiting_approval 后测试变红 | Passed |
| 已读按 `item_key` 幂等且跨用户隔离 | 重复标记报错，或读到别人的已读 | `WorkItemView` 唯一约束、`mark_viewed`、`viewed_keys` | repository 测试两次标记同一 key，另一用户集合为空；真实 HTTP 标记后 summary.unread=0 | 去掉 uid 条件或唯一约束后测试变红 | Passed |
| 待办只对 Run 所属用户可见 | 通过 URL 猜别人的 Run | `get_latest_run` 的 `uid` 条件 | 两个真实用户 integration：第二个用户 total=0；旧 key 404 | 去掉 uid 条件后第二个用户能看到则变红 | Passed |
| 定时任务发起方显示为数字员工 | 定时 Run 被显示为用户手工发起 | `work_item_to_dto` | 真实 HTTP integration：`source=scheduled_agent` 的 item 为 `initiator_type=digital_employee`、名称取 Agent | 删除 scheduled 分支后测试变红 | Passed |
| 筛选、分页和 total 来自同一窗口结果 | 先分页再筛选导致 total 与列表不一致 | `list_latest` 的共用 `latest` 子查询 | 真实 HTTP：`limit=1` 时列表 1 条、total=2；`status=waiting_approval` 时 total=1 | 把 status 过滤放到分页之后后测试变红 | Passed |
| 前端能展示真实待办并跳转现有会话 | 页面仍是静态演示，或审批跳转到新页面 | `WorkbenchView.vue`、`AppLayout.vue`、`utils/workItem.js` | `node --test src/utils/workItem.test.js`：5 passed；eslint 0；`vite build` 通过 | 把 waiting_approval 动作改成打开抽屉内审批后 util 测试变红；真实浏览器交互未执行 | Partially passed |
| 旧库升级到 v11 可重复执行 | 已有库启动时缺表或重复升级失败 | `models_business.py`、`manager.py`、`storage_migration.py` | 隔离真实 PostgreSQL 迁移测试：建表、唯一索引、连续两次 `ensure_business_schema`；schema 版本测试 13 passed | 删除 DDL 或唯一索引后迁移测试变红 | Passed |

最低证据：repository/service unit 覆盖窗口、状态、DTO、已读；真实 HTTP integration 覆盖两个用户、筛选、分页、旧 key 和定时发起方；真实 PostgreSQL 覆盖 v10→v11 迁移；前端 unit/lint/build 通过。未执行确定性 E2E（replay 网关 `http://localhost:8765/health` 不可达）和真实浏览器交互。

## 风险

- **已读行生命周期**：当前没有随 Run 清理回收 `work_item_views`；长期运行会留下孤儿行。规模变大前需要按 `item_key` 清理或在 Run 清理事务中删除。
- **窗口查询成本**：`uid + run_type + source` 过滤依赖现有索引；单用户线程数很大时需要补 `(uid, conversation_thread_id, created_at)` 复合索引，当前未加。
- **审批摘要不是审批内容**：待办只展示等待类型，用户必须进入会话；如果未来要求在收件箱直接审批，需要重新设计 approval payload 的所有权和 resume 边界。
- **`scheduled_agent` 之外的自动来源**：目前只把 `scheduled_agent` 映射为数字员工；新增来源时需要明确发起方，否则会回退成当前用户。
- **P3 双源分页**：P2 的窗口查询只投影 Run；P3 加 `handoff` 时必须在 SQL union 后统一排序和分页，不能先分别分页再合并。
