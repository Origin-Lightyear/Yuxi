# 数字员工 AI 会议

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/meeting_service.py

## 问题

原型 P6 要求数字员工围绕议题进行多轮会议，人可以在会中插话，最终把需要负责人决定的事项挂到「待拍板」。如果为 AI 会议再建一套会议表、消息表和决议表，P4 分身会议会与 P6 形成两套并行事实：同一场会议、同一条发言、同一个签字动作需要两处维护；如果会议只生成一段 Markdown，没有结构化决议和签字人，页面上的「通过」仍然只是演示。

## 决策

### 实现方案

P6 复用 P4 的 `Meeting` / `MeetingMessage` / `MeetingDecision` / `MeetingDecisionSigner`，只用 `Meeting.kind='digital_employee'` 区分。创建会议时校验参与者是当前用户可见且在岗的数字员工；`meeting_service.py` 把每位数字员工的岗位、职责、能力、边界和验收整理成模型输入，调用系统默认聊天模型生成结构化 JSON，再校验参与者、轮次、引用和决议数量。生成失败写 `generation_error`、保留 `draft` 并返回 502；不写空纪要、不伪装成功。

人入会通过 `meeting_messages` 写入 `speaker_type='human'` 的发言，轮次默认当前会议轮次；管理员、创建人、参与者负责人和已被加入 `human_uids` 的人可以发言。会议详情返回 `can_join` 和 `can_manage`，前端据此展示入会入口，但最终授权在 service。

AI 会议不创建 `AgentRun`：会议是管理协作事实，不是聊天执行；不占用 FIFO 队列、worker lease 或 SSE Run 链路。会议页 `/meetings` 使用共享 `MeetingWorkspace`，支持创建草稿、生成纪要、查看发言、人工入会、查看待签决议和历史会议列表。该页面当前作为隐藏深链保留，侧边栏不展示「AI 会议」入口；后端接口和已有深链继续可用。

### 权限、失败与数据边界

- 列表和详情只返回当前用户可见的会议；普通用户看不到未受邀会议。
- 创建要求管理员或至少一位参与者负责人；生成和人工推进要求管理员或会议管理者。
- 参与者必须是在岗数字员工；已停用员工不能在创建时被加入，也不能在列表中被伪装成可参会。
- 会议生成使用 `system_options.default_model`；没有默认模型或模型返回结构错误都显式失败，不产生空决议。
- 决议签字人默认取仍在岗的参与者归属人并去重，归属人已删除或没有归属人时回落到创建人；创建请求可显式传 `signer_uids`。
- 人工参会人和指定签字人都可以入会发言；模型发言和人工发言的轮次都限制在会议配置的 `rounds` 内。
- 待签决议沿用 P4 的待办投影，AI 会议与分身会议共用 `decision:<id>` 来源。

## 替代方案

- **为 AI 会议新建独立表**：可以让字段更贴合数字员工，但会复制发言、决议、签字和待办投影；同一产品能力出现两套事实。
- **只保留会议摘要 Markdown**：实现简单，但无法按决议签字、拒绝或生成待办，也无法在页面区分「讨论」和「已拍板」。
- **会议直接创建 AgentRun**：能复用执行链路，但会议不是一次工具执行，Run 的 lease、SSE 和产物语义反而增加负担。
- **允许任意数字员工参与**：会绕过员工可见范围和停用状态；参与者必须在创建时按当前用户权限校验。

## 后果

- AI 会议依赖系统默认聊天模型；模型不可用时会议无法生成，但草稿和错误可观察、可重试。
- 会议生成为同步 HTTP 调用，超时受网关和模型供应商影响；当前没有后台任务或流式进度，长会议可能触发请求超时。
- `meeting_messages.citations` 对数字员工为空；当前只校验分身经验引用，数字员工岗位档案作为整体上下文，没有条目级引用。
- 会议不会自动通知参与者或签字人；待签事项通过 P2 待办被发现。
- 会议与分身会议共用状态和签字人模型，后续若数字员工会议需要不同签字规则，必须在 `Meeting.kind` 分支内显式扩展，不能复制表。
- 生成结果中的决议由模型提议，签字人仍需逐条判断；生效决议只对分身回写草稿经验，AI 会议不回写经验。
- 创建页面没有选择人工参会人和显式签字人的控件；创建人、管理员和指定签字人可直接入会，其他人工参会人需要由 API 创建会议时传入 `human_uids`。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| AI 会议复用 P4 会议事实，仅以 `kind` 区分 | 新建并行会议表或前端假数据 | `Meeting.kind`、`meeting_service.py` | `test/unit/services/test_meeting_service.py` 9 passed；真实 HTTP 会议测试覆盖 twin 与数字员工创建、详情和签字 | 把 `kind` 约束放开或复制表后迁移/服务测试失败 | Passed |
| 参与者必须是当前用户可见且在岗的数字员工 | 停用员工或被隐藏员工进入会议 | `_load_participant_agents`、`digital_employee_is_visible` | 真实 HTTP 测试创建在岗员工会议成功，草稿员工创建返回 409；service 仍做同一校验 | 传入普通 Agent 返回 422；草稿员工返回 409 | Passed |
| 生成失败保留草稿并返回 502，不写空纪要 | 模型失败被当作成功，或草稿被清空 | `run_meeting`、`MeetingGenerationError` | `test/unit/services/test_meeting_service.py` 覆盖生成结构校验和失败保留草稿；真实模型路径受环境限制 | 模型返回非 JSON/无发言/无摘要时抛出 `MeetingGenerationError` | Passed（unit） |
| 人可以入会并写入带轮次的发言 | 未受邀普通用户发言，或发言未绑定会议 | `add_human_message`、`can_join` | 真实 HTTP 测试断言人工发言出现且 `can_join=true`；签字人和人工参会人共用 `can_join` | 非管理者且不在 `human_uids`/`signer_uids` 返回 403；超出会议轮次返回 422 | Passed |
| 待拍板决议进入待办，历史会议和签字结果可查 | 页面只显示静态会议，或签完状态不更新 | `meeting_router.py`、`MeetingWorkspace.vue`、`work_item_repository.py` | meeting HTTP 测试验证签前 `waiting_approval`、签后 `completed`、拒绝后 `failed`、非签字人不可见；前端 meeting/workItem unit 通过 | 非签字人列表出现决议 key 或拒绝后仍待签时测试失败 | Passed |
| 前端 `/meetings` 页面可创建、生成、入会、签字和历史查看 | 编译失败或仍用静态演示 | `MeetingView.vue`、`MeetingWorkspace.vue`、`meetingApi.js` | ESLint 0；`node --test` 全量 491 passed；`vite build` 成功 | `MeetingComp` 路由、会议详情和决议跳转由前端 unit/构建覆盖 | Passed |
| 全量后端 unit 无回归 | P4–P6 改动破坏既有 unit | 全仓库 | `pytest test/unit -m "not slow"`：2721 passed, 58 skipped | 无 | Passed |

## 未运行 / 部分运行

- 真实模型驱动的会议生成未在 live API 跑通：当前环境默认聊天模型不可用；unit 覆盖生成校验和失败保留草稿，真实 HTTP 覆盖签字、待办投影和经验回写。
- 真实浏览器页面交互与截图未执行；完成 ESLint、前端全量 unit 和 `vite build`，但没有可用登录会话和浏览器自动化。
- 会议行锁未做真实 PostgreSQL 并发压测；SQLite unit 只验证状态机和失败路径，不证明并发隔离。
- `uv run ruff check package` 未运行：容器内未安装 ruff（`Failed to spawn: ruff`）；没有用其他 lint 替代。
- 全新上下文的 Reviewer 未执行：子代理额度耗尽；当前证据来自实现者自审、自动化测试和手工复核。
- 通用 deterministic E2E 的 12 项仍 skip，当前 SaaS 模式缺少测试租户 `E2E_USERNAME`/`E2E_PASSWORD`。
