# 员工技能卡与交接看板

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/employee_skill_service.py

## 问题

原型 P5 把「技能」理解为员工可沉淀、可交接的经验包：步骤、判断规则、能力边界、验收标准和沉淀方式要能被同事接手。仓库已有的 `skills` 表是 Agent Skill 的元数据，绑定文件、工具和运行时加载，不具备员工归属、审阅状态或交接状态。直接复用会让普通员工技能混进 Agent 运行时，也会让现有 Skill 文件被工作台改写。

## 决策

### 实现方案

新增 `employee_skills` 表，独立于 Agent `skills`。`employee_skill_service.py` 拥有状态机、可见性和交接规则；`employee_skill_repository.py` 负责按状态、交接状态和关键词查询；`employee_skill_router.py` 只做 HTTP 模型、身份依赖和 DTO。前端 `/employee-skills` 页面提供技能卡列表、详情抽屉、创建/编辑、送审、确认、归档、指派接手人和交接看板。

状态机：

```text
draft ──submit──> needs_review ──verify──> ready ──edit──> needs_review
  │                    │                    │
  └──────verify────────┴────────────────────┘
                       │
                       └──archive──> archived
```

交接状态独立于内容状态：`unassigned -> assigned -> accepted`。只有已就绪技能能进入交接；指派由归属人或管理员执行；确认接手只能由指定接手人或管理员执行。已归档技能不再对所有员工展示，只保留给归属人、接手人和管理员。

### 可见性与权限

- 归属人和管理员可以创建、编辑、送审、确认、归档、指派和删除未交接草稿。
- 所有登录员工可以查看 `ready` 技能；`draft`、`needs_review`、`archived` 只对归属人、指定接手人和管理员可见。
- 不可见统一 404，可见但无管理权限返回 403；前端隐藏按钮不构成授权边界。
- 管理员可以代建技能并指定 `owner_uid`，普通员工只能创建自己的技能。
- 接手人候选名单只对技能管理者开放，且排除当前归属人；避免普通员工通过接口枚举用户目录。

### 内容与收敛

技能卡保存标题、部门、沉淀方式、步骤、规则、边界、验收和来源引用。`interview`、`recording`、`process`、`review` 四种沉淀方式是 API 约束，不是前端自由文本。确认就绪前必须至少有标题、步骤和验收；已就绪内容被修改后退回 `needs_review`，避免页面继续显示「已就绪」而实际内容已变。

## 替代方案

- **复用 Agent `skills` 表**：已有运行时加载、文件生命周期和权限语义，但员工技能没有 Agent 归属，也不需要进入 Agent 工具链；复用会污染两组事实。
- **只做文本知识库条目**：实现快，但没有归属、审阅、交接和「谁已接手」的责任状态。
- **把交接状态塞进 JSON**：查询和待办投影都需要按状态过滤；独立列和索引让状态成为可验证事实。
- **草稿也对全员可见**：方便发现，但会暴露未审阅内容并模糊「已就绪」的可信边界。

## 后果

- `employee_skills` 与 Agent `skills` 是两套事实，页面和 API 不能互相替代；后续如果要把已就绪技能挂到数字员工，需要单独的映射决策。
- 可见性在 service 层逐条过滤，当前规模按几十到几百张卡设计；数量上来后需要把权限过滤下推到 SQL，但不能复制权限规则。
- 交接候选只返回 uid、姓名和部门 ID，不暴露完整用户档案；管理员代建时仍需知道目标 uid。
- 技能内容修改会使已就绪卡退回送审，已接手的技能也会因此重新进入待确认；这是内容可信优先于交接状态的取舍。
- 草稿删除只允许在未进入交接时执行，避免删除已承诺给接手人的事实。
- 所有状态变更先锁定技能行并刷新已加载字段，避免并发送审、确认、归档或交接互相覆盖；行锁不替代后端权限校验。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| v13 技能表、状态约束和索引幂等 | 旧库缺表或约束错误 | `models_business.py`、`manager.py`、`storage_migration.py` | `test/integration/storage/test_founder_twin_migration.py` 1 passed；表、约束和索引均有断言 | 缺表、缺约束或重复执行时迁移测试失败 | Passed |
| 归属人/管理员可管理，普通员工只读已就绪技能 | 草稿被全员看到，员工越权编辑/确认 | `employee_skill_service.py::can_view_skill`、`get_manageable_skill` | `test/integration/api/test_employee_skill_api.py` 1 passed；`test/unit/services/test_employee_skill_service.py` 4 passed | 同事读取草稿 404、确认草稿 404、修改已就绪技能 404 | Passed |
| 状态机与交接状态按后端事实推进 | 直接改请求正文跳过送审/确认，或草稿进入交接 | `submit_skill_for_review`、`verify_skill`、`assign_handover`、`accept_handover` | 真实 HTTP 测试覆盖 draft→ready→assigned→accepted→needs_review；unit 4 passed | 未就绪技能指派返回 422；非指定接手人确认返回 403；重复编码返回 409 | Passed |
| 交接候选只对管理者开放且排除归属人 | 普通员工枚举用户，或把技能指派给自己 | `employee_skill_router.py::list_handover_candidates`、`assign_handover` | 真实 HTTP 测试断言候选含接手人、不含归属人；同事请求返回 403 | 无管理权限时候选接口返回 403 | Passed |
| 技能管理页面可创建、编辑、送审、确认、归档和看板交接 | 页面仍展示静态示例或编译失败 | `EmployeeSkillsView.vue`、`employeeSkillApi.js`、`employeeSkill.js` | ESLint 0；`node --test` 全量 491 passed；`vite build` 成功 | 状态映射和交接 API 由前端 unit/构建覆盖 | Passed |
| 全量后端 unit 无回归 | P4–P6 改动破坏既有 unit | 全仓库 | `pytest test/unit -m "not slow"`：2721 passed, 58 skipped | 无 | Passed |

## 未运行 / 部分运行

- 真实浏览器页面交互与截图未执行；完成 ESLint、前端全量 unit 和 `vite build`，但没有可用登录会话和浏览器自动化。
- 交接候选接口只验证了真实 HTTP 的权限和返回字段；没有对大规模用户目录做性能测试。
- 技能行锁未做真实 PostgreSQL 并发压测；SQLite unit 只验证状态机与错误分支，不证明并发隔离。
- `uv run ruff check package` 未运行：容器内未安装 ruff（`Failed to spawn: ruff`）；没有用其他 lint 替代。
- 全新上下文的 Reviewer 未执行：子代理额度耗尽；当前证据来自实现者自审、自动化测试和手工复核。
