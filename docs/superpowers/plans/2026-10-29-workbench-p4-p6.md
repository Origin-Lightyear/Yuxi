# 企业智能工作台 P4–P6 实施计划

状态：implemented（2026-10-30）

来源原型：`大力水手Agent-企业智能工作台.html`（未跟踪的静态演示）

范围：

- P4「分身协作」：跨分身请教、分身会议、决议确认/签字。
- P5「技能管理」：员工技能卡、沉淀方式、交接看板和接手状态。
- P6「AI 会议」：数字员工多轮会议、人可入会、待拍板、历史会议。

本计划承接 `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md` 的 P1–P3，不推翻既有边界：
会议和技能是新的产品事实，不伪装成静态示例；权限、状态和引用由后端拥有。

## 1. 验收主张

### P4

- 老板分身在一次咨询里可以只读方式请教另一个分身；返回的目标分身经验必须已确认、属于目标分身，并标记「本人未复核」。
- 跨分身引用和本分身引用分开存储、分开展示；最终答案仍必须经过 `submit_consultation_answer`，答案守卫不放松。
- 分身会议可以创建、生成纪要、人工插话、形成决议；决议只有指定负责人能签字。
- 决议在全部负责人同意后生效；任一负责人拒绝则拒绝；生效决议回写一条草稿经验，不自动确认。
- 待签决议进入 P2 待办，按签字人可见；签完后状态进入完成/失败。

### P5

- 管理员或技能归属人可以创建、编辑、送审、确认技能卡；普通员工只能看已就绪技能。
- 技能卡包含步骤、规则、边界、验收和沉淀方式；四种沉淀方式在 API 和页面都能表达。
- 交接看板能按未分配、已指派、已接手分组；只有归属人/管理员能改派，接手人/管理员能确认接手。
- 技能卡状态由后端状态机拥有，前端不通过隐藏按钮代替授权。

### P6

- 数字员工会议复用 P4 的会议事实，不复制一套并行表。
- 会议参与者必须是当前用户可见且在岗的数字员工；生成纪要使用真实模型调用，失败显式返回错误并保留草稿。
- 人可以作为参与者向会议写入发言，发言绑定会议和轮次。
- 会议历史、纪要、待签决议和签字结果在 AI 会议页可查。

## 2. 数据与边界

- `meetings`：会议事实，`kind in ('twin','digital_employee')`，保存主题、参与者、轮次、状态、纪要摘要、共识、分歧。
- `meeting_messages`：分身/数字员工/人的发言，引用以 JSON 保存条目 ID。
- `meeting_decisions`：会议决议，保存提议者、正文、影响和生效文本。
- `meeting_decision_signers`：每个决议的指定签字人及签字状态，避免把签字列表塞进 JSON 后无法做待办投影。
- `employee_skills`：员工技能卡，不是已有 Agent `skills` 表；二者语义不同。
- 会议生成只写会议表；不创建 AgentRun。会议是管理协作事实，不是聊天执行。
- 决策待办由 `work_item_repository` 运行时投影，不新增第二套待办状态表。
- 跨分身引用只读目标分身已确认经验，不暴露目标分身草稿、配置和未确认内容。

## 3. 实施顺序

### Task 1：共享会议事实

- [x] 增加 `Meeting`、`MeetingMessage`、`MeetingDecision`、`MeetingDecisionSigner` 模型。
- [x] 在 `storage/postgres/manager.py` 增加表的幂等 DDL，提升 business schema 版本。
- [x] 增加 `meeting_repository.py`，集中会议、发言、决议、签字查询。
- [x] 增加迁移测试，验证表、约束和幂等执行。

验证：`docker compose exec -T api uv run --no-sync --group test pytest test/integration/storage/test_founder_twin_migration.py -q`

### Task 2：会议服务与 API

- [x] `meeting_service.py`：创建、列表、详情、生成纪要、人工发言、签字、生效回写。
- [x] `meeting_router.py`：`/api/meetings` 的创建、列表、详情、run、message、decision sign。
- [x] 生成器使用 `system_options.default_model`，JSON 修复后做参与者和引用校验；失败保留 draft。
- [x] 会议权限：管理员、创建人、参与者归属人、指定签字人可见；创建/生成/人工发言要求管理员或参与者管理者。
- [x] 决议签字只允许指定签字人；全部同意生效，任一拒绝失败；生效时创建草稿经验。
- [x] 单元测试覆盖生成校验、权限、签字状态机和回写。

验证：`docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_meeting_service.py test/integration/api/test_meeting_api.py -q`

### Task 3：P4 跨分身请教

- [x] `ExperienceConsultation` 增加 `external_citations` JSON。
- [x] 增加 `ask_another_twin` 工具，只读检索目标分身已确认经验，禁止自我请教和越权目标。
- [x] `submit_consultation_answer` 接受外部引用，校验引用来自本轮 `ask_another_twin` 结果。
- [x] 咨询记录和前端展示区分本分身依据与外部未复核口径。
- [x] 负向测试：目标分身不存在/不可见、草稿经验、未先请教就外部引用、自我请教。

验证：`docker compose exec -T api uv run --no-sync --group test pytest test/unit/agents/test_founder_twin_tools.py test/unit/services/test_founder_twin_service.py -q`

### Task 4：P2 待办接入决议

- [x] `WorkItemRepository` 增加决议签字投影，一个签字人一条待办。
- [x] `SOURCE_TYPES`、key 解析、详情和前端跳转支持 `decision`。
- [x] 待办测试覆盖待签、同意、拒绝、非签字人不可见。

验证：`docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_workbench_service.py test/integration/api/test_workbench_api.py -q`

### Task 5：P5 技能管理

- [x] 增加 `EmployeeSkill` 模型和 DDL。
- [x] `employee_skill_service.py`：状态机、权限、交接指派/接手、筛选。
- [x] `employee_skill_router.py`：列表、详情、创建、更新、送审、确认、指派、接手。
- [x] 单元和真实 HTTP 测试覆盖普通员工只读、归属人管理、接手人确认、草稿不可见。

验证：`docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_employee_skill_service.py test/integration/api/test_employee_skill_api.py -q`

### Task 6：前端

- [x] `meetingApi.js`、`employeeSkillApi.js` 和对应 utils。
- [x] `MeetingWorkspace.vue` 共享会议界面；老板分身页增加「分身会议」页签，新增 `/meetings` AI 会议页。
- [x] 新增 `/employee-skills` 技能管理页，包含列表、详情、编辑、交接看板。
- [x] 路由和侧边导航增加「AI 会议」「技能管理」。
- [x] 待办来源、动作和跳转支持决议。
- [x] 前端 unit 覆盖状态文案、跳转和 API 边界。

验证：

```bash
cd web
./node_modules/.bin/eslint src test --max-warnings=0
node --test --test-concurrency=1 "test/**/*.test.js" "test/**/*.spec.js"
./node_modules/.bin/vite build
```

### Task 7：文档与收口

- [x] 新增三份决策记录，写清问题、决定、替代项、后果和验证。
- [x] 更新总设计文档状态和 P4–P6 边界。
- [x] 运行工程契约、后端 unit、相关 integration、前端 gate、`git diff --check`。
- [ ] 全新上下文 Reviewer 审查完整需求、diff、测试和规范（未执行：子代理额度耗尽；当前由实现者自审覆盖）。

## 4. 反目标

- 不把会议做成静态演示或前端拼接的假纪要。
- 不把决议签字放进 JSON 后另建一套待办同步。
- 不因新增技能管理复用或污染 Agent Skills 表。
- 不让会议生成失败伪装成空会议成功。
- 不让普通员工通过改请求正文给未就绪技能送审、确认或改派。
