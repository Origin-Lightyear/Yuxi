# 跨分身请教与分身会议

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/founder_twin.py

## 问题

P3 的老板分身只能引用本分身已确认经验。原型 P4 要求分身之间可以互相请教，并把多个分身组织成一场会议形成可签字决议。直接允许分身递归调用另一个 Agent 会带来三个问题：被请教分身获得完整会话和工具权限，一次咨询可能变成无限互调；外部口径与本分身依据混在同一个引用列表里，负责人无法判断哪条已被目标分身确认；会议如果没有持久化决议和签字人，页面上的「通过」只是文案。

## 决策

### 实现方案

跨分身请教在 `founder_twin.py` 内完成，复用同一咨询 Run 和同一个 `submit_consultation_answer` 工具，不启动第二个 Agent。`ask_another_twin` 只读加载目标分身可见性和已确认经验，返回带 `review_status=unreviewed` 的结果；外部引用只能引用本轮工具实际返回的 `(twin_slug, entry_id)` 对。外部引用写入 `experience_consultations.external_citations`，与本分身 `cited_entry_ids` 分开校验、存储和展示。

会议事实由 `meetings`、`meeting_messages`、`meeting_decisions`、`meeting_decision_signers` 四张表拥有。`meeting_service.py` 负责创建、生成、人工发言和签字状态机；`meeting_repository.py` 负责查询和写事实；`meeting_router.py` 只做身份、DTO 和状态码。会议生成失败通过 `generation_error` 保留草稿并返回 502，不写空纪要。决议全部指定签字人同意才生效；任一拒绝则整条决议拒绝。生效决议按 `proposer_slug` 回写一条 `draft` 经验，`source_type='meeting'`，不自动确认。

待签决议由 `work_item_repository` 运行时投影为 `decision:<id>` 待办，每个签字人一条；状态映射 `pending -> waiting_approval`、`approved -> completed`、`rejected -> failed`。DTO 增加 `meeting_id`，前端才能从待办跳到 owning 会议。

### 权限与边界

- 同一轮最多向两个不同分身请教；不能向自己请教；已请教过的目标可继续提问。
- 目标分身必须对当前用户可见且 `kind='founder_twin'`；不可见和不存在的目标统一返回失败结果，不透露存在性。
- 答案至少要有本分身引用或外部引用之一；外部引用本身也是经过目标分身确认的经验，守卫继续要求最终文本来自成功提交。
- 会议只对管理员、创建人、参与者归属人、人工参会人和指定签字人可见；生成纪要、人工推进要求管理员或参与者负责人；签字只允许该决议的指定签字人。
- 分身会议参与者只能是老板分身；AI 会议参与者只能是当前用户可见且在岗的数字员工。会议不创建 AgentRun，不占用聊天执行链路。
- 人工参会人和指定签字人都可以入会发言；`can_join` 由后端返回，前端不自行推断。

## 替代方案

- **让分身递归调用目标 Agent**：能复用完整对话能力，但会继承工具、模型和上下文，且难以限制递归深度和成本；只读经验检索足够表达「请教」。
- **外部引用和本分身引用合并成一个列表**：实现简单，但无法区分哪些已被本分身负责人确认；分表字段和分开展示保留了责任边界。
- **把会议纪要直接写成 Markdown 文档**：可读性好，但无法做决议级签字、拒绝和待办投影；结构化 JSON 生成、数据库事实和页面渲染分离。
- **会议直接生成已生效决议**：少一步操作，但把模型输出当成负责人决策；必须保留指定签字人。
- **新建会议专用待办表**：查询方便，但会和 P2 的运行时投影形成第二事实源；继续用 union 投影。

## 后果

- 跨分身回答只代表目标分身已确认经验，本分身负责人尚未复核；页面必须持续显示「本人未复核」，不能把它升级成组织结论。
- 外部引用增加一次数据库检索和最多两个目标的上下文，但不会递归执行 Agent；成本可控。
- 会议生成依赖系统默认聊天模型。模型不可用或返回结构错误时会议保持草稿，用户可重试；不会产生空决议或假生效。
- 生效决议回写草稿经验，仍需分身负责人确认后才进入检索；会议本身不会自动扩大知识库可信范围。
- 待办投影按签字人展开，同一决议可能产生多条待办；这是签字责任模型，不是重复任务。
- 会议签字默认按仍在岗的参与者归属人去重；归属人已删除或没有归属人时回落到创建人。若参与者归属人不等于实际决策人，需要在创建时显式传 `signer_uids`，页面暂未提供该控件。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 跨分身只读检索目标分身已确认经验，并标记未复核 | 递归启动 Agent、返回草稿经验或目标分身配置 | `founder_twin.py::_ask_another_twin` | `test/unit/services/test_meeting_service.py` 9 passed，含外部引用成功写入；`test/unit/agents/test_founder_twin_tools.py` 5 passed | 自我请教、不可见目标、未先请教的外部引用、引用目标分身草稿均失败 | Passed |
| 外部引用与本分身引用分开持久化和展示 | 外部引用被当作本分身 confirmed 依据 | `ExperienceConsultation.external_citations`、`_submit_answer`、`FounderTwinView.vue` | tool unit 断言 `external_citations`；真实 HTTP 咨询列表由既有 founder API 测试覆盖；展示层跳过非法持久化条目 | 未先请教的外部引用被拒绝；外部条目标错目标分身被拒绝 | Passed |
| v12→v13 会议表、签字人、技能表和外部引用列幂等 | 旧库缺表、约束错误或重复升级失败 | `models_business.py`、`manager.py`、`storage_migration.py` | `test/integration/storage/test_founder_twin_migration.py` 1 passed；表、命名外键、索引和 schema 版本均有断言 | 迁移测试对缺表、缺约束和重复执行断言失败 | Passed |
| 决议只有指定签字人能签，全部同意才生效，任一拒绝失败 | 非签字人越权、部分同意即生效、拒绝仍生效 | `meeting_service.py::sign_decision` | `test/integration/api/test_meeting_api.py` 3 passed；`test/unit/services/test_meeting_service.py` 9 passed | 普通用户签字返回 403/404；拒绝后会议 `rejected` 且无经验回写 | Passed |
| 生效决议回写草稿经验，拒绝不回写 | 自动确认经验或拒绝也写经验 | `_finalize_experience` | meeting HTTP 测试断言 `experience_entry_id` 非空且经验 `status=draft`；拒绝测试断言为空 | 回写状态写成 confirmed 或拒绝分支写经验时 HTTP 断言失败 | Passed |
| 待签决议进入 P2 待办并按签字人可见 | 非签字人看到待办，签完仍显示待签 | `work_item_repository.py::_decision_select`、`web/src/utils/workItem.js` | `test/unit/services/test_workbench_service.py` 6 passed；meeting HTTP 测试验证签前 `waiting_approval`、签后 `completed`、拒绝后 `failed`、非签字人列表不出现该 key | 非签字人列表出现该 key 或拒绝后仍为待签时测试失败 | Passed |
| 分身会议页面可创建、生成、入会、签字，并展示历史会议 | 前端编译失败或仍用静态演示 | `MeetingWorkspace.vue`、`FounderTwinView.vue`、`meetingApi.js` | ESLint 0；`node --test` 全量 491 passed；`vite build` 成功 | 会议页签、`decision` 跳转和会议详情请求均有前端断言或代码路径 | Passed |
| 全量后端 unit 无回归 | P4–P6 改动破坏既有 unit | 全仓库 | `pytest test/unit -m "not slow"`：2721 passed, 58 skipped | 无 | Passed |

## 未运行 / 部分运行

- 真实模型驱动的会议生成未在 live API 跑通：当前环境默认聊天模型不可用；unit 覆盖生成校验和失败保留草稿，真实 HTTP 覆盖签字、待办投影和经验回写。
- 真实浏览器页面交互与截图未执行；完成 ESLint、前端全量 unit 和 `vite build`，但没有可用登录会话和浏览器自动化。
- 会议行锁和技能行锁只在实现中启用，未做真实 PostgreSQL 并发压测；SQLite unit 只能验证状态机，不证明并发隔离。
- `uv run ruff check package` 未运行：容器内未安装 ruff（`Failed to spawn: ruff`）；没有用其他 lint 替代。
- 全新上下文的 Reviewer 未执行：子代理额度耗尽；当前证据来自实现者自审、自动化测试和手工复核。
- 通用 deterministic E2E 的 12 项仍 skip，当前 SaaS 模式缺少测试租户 `E2E_USERNAME`/`E2E_PASSWORD`。
