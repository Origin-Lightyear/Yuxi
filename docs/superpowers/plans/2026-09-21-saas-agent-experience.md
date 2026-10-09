# SaaS Agent 体验与租户能力适配实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Agent 展示、SaaS 用户资料与设置、退出登录、NewAPI 模型缓存和租户知识库权限，使 SaaS 员工获得一致且受权限约束的 Agent 体验。

**Architecture:** 沿用现有 Vue/Pinia 页面状态、FastAPI 认证路由、PostgreSQL 模型供应商与知识库事实数据。前端只负责展示和入口控制，后端继续作为认证、模型缓存和租户知识库权限的最终边界；不新增 capabilities 接口或远程知识库代理。

**Tech Stack:** Vue 3、Pinia、Vue Router、Node test runner、FastAPI、SQLAlchemy、pytest、Redis 模型缓存、Docker Compose。

**Spec:** `docs/vibe/2026-09-21-saas-agent-experience-design.md`

## Global Constraints

- 保留工作区已有未提交修改，不回滚与本需求无关的文件。
- 前端 API 调用继续集中在 `web/src/apis`，样式继续使用 Less 和现有 CSS 变量。
- 后端路由保持薄，知识库授权继续使用现有 `share_config`、`ResourcePermission` 和员工权限依赖。
- SaaS 员工内部角色仍为 `user`，仅隐藏本地角色文案和管理员设置入口。
- NewAPI 拉取失败时保留上一版模型，不用空列表覆盖数据库记录。
- 所有生产代码变更前先写对应失败测试，并实际运行确认失败。

---

### Task 1: Agent 推理与当前任务自动收起

**Files:**
- Modify: `web/src/components/AgentMessageComponent.vue`
- Modify: `web/src/components/ToolCallsGroupComponent.vue`
- Modify: `web/src/components/ToolCallingResult/tools/TaskTool.vue`
- Test: `web/test/unit/agentMessagePresentation.test.js` (create)

**Interfaces:**
- Consumes: `message.status`, `MessageProcessor.parseAssistantMessageBody`, `activeSubagentToolCallIds`。
- Produces: 可由消息组件和工具组复用的“当前活动项”判断；完成态组件默认收起，活动项最多一个自动展开。

- [ ] **Step 1: 写失败测试**

  在 `web/test/unit/agentMessagePresentation.test.js` 提取纯函数测试所需的状态规则，覆盖：

  ```js
  test('completed reasoning is collapsed by default', () => {
    assert.deepEqual(resolveReasoningPresentation({ status: 'finished' }), {
      active: false,
      expanded: false
    })
  })

  test('only the active task is expanded while a run is executing', () => {
    const state = resolveToolPresentation([
      { id: 'old', status: 'success' },
      { id: 'current', status: 'running' }
    ])
    assert.deepEqual(state.filter((item) => item.expanded).map((item) => item.id), ['current'])
  })
  ```

  导出或抽取最小的展示规则函数，不在测试中复制组件实现。

- [ ] **Step 2: 运行测试确认正确失败**

  Run: `cd web && pnpm test:unit --test-name-pattern="reasoning|active task"`

  Expected: FAIL，因为当前没有统一的活动项解析函数，且组件内部状态无法满足“最多一个自动展开”。

- [ ] **Step 3: 实现最小状态规则**

  将推理/工具自动展开规则集中到轻量纯函数（可放在现有组件旁的同目录工具文件），并让组件：

  - 运行中只根据活动工具或 `activeSubagentToolCallIds` 展开当前项；
  - 活动项切换时收起旧项；
  - `finished`、`error`、`cancelled` 时重置自动展开状态；
  - 用户手动展开完成项时不被无关消息更新覆盖。

  不改后端事件协议，不删除用户手动展开入口。

- [ ] **Step 4: 运行测试确认通过**

  Run: `cd web && pnpm test:unit --test-name-pattern="reasoning|active task"`

  Expected: PASS，且现有 `agentRun`、`messageProcessor` 测试不回归。

- [ ] **Step 5: 提交任务变更**

  ```bash
  git add web/src/components/AgentMessageComponent.vue web/src/components/ToolCallsGroupComponent.vue web/src/components/ToolCallingResult/tools/TaskTool.vue web/test/unit/agentMessagePresentation.test.js
  git commit -m "fix: 收起已完成的 Agent 推理任务"
  ```

### Task 2: SaaS 用户资料字段与设置入口

**Files:**
- Modify: `web/src/stores/user.js`
- Modify: `web/src/components/UserInfoComponent.vue`
- Modify: `web/src/components/SettingsModal.vue`
- Modify: `web/src/components/AccountSettingsComponent.vue` (only if profile labels are assembled here)
- Test: `web/test/unit/userExperience.test.js` (create)

**Interfaces:**
- Consumes: 登录和 `/api/auth/me` 返回的 `saas_mode`、`employee_code`、`department_name`、`username`。
- Produces: `userStore.saasMode` 和统一的 `displayName`/`displayDepartment` 展示数据；设置导航按 SaaS 身份收口。

- [ ] **Step 1: 写失败测试**

  覆盖登录保存 SaaS 字段、`getCurrentUser` 更新 SaaS 字段，以及 SaaS 员工资料不渲染本地角色：

  ```js
  test('stores saas identity fields from login response', async () => {
    const store = createUserStoreWithFetch({ saas_mode: true, username: '张三', department_name: '研发部', employee_code: 'E01', role: 'user' })
    await store.login({ loginId: '13800000000', password: 'password' })
    assert.equal(store.saasMode, true)
    assert.equal(store.departmentName, '研发部')
    assert.equal(store.employeeCode, 'E01')
  })

  test('saas profile uses department and employee name instead of local role', () => {
    assert.equal(formatUserSummary({ saasMode: true, departmentName: '研发部', username: '张三', userRole: 'user' }), '研发部 · 张三')
  })
  ```

- [ ] **Step 2: 运行测试确认失败**

  Run: `cd web && pnpm test:unit --test-name-pattern="saas identity|saas profile"`

  Expected: FAIL，因为 `getCurrentUser` 未完整恢复 SaaS 字段且资料组件仍使用本地角色摘要。

- [ ] **Step 3: 实现最小前端改动**

  - 在 `getCurrentUser` 和初始化路径同步 `saasMode`、`employeeCode`；
  - UserInfo 菜单对 SaaS 员工显示“部门名 · 员工姓名”，部门为空时只显示员工姓名；
  - SettingsModal 对 SaaS 员工只保留账户资料；
  - 不改变后端本地管理员权限判断。

- [ ] **Step 4: 运行测试确认通过**

  Run: `cd web && pnpm test:unit --test-name-pattern="saas identity|saas profile"`

  Expected: PASS；再运行 `pnpm test:unit --test-name-pattern="database|agent"` 确认共享 store 无回归。

- [ ] **Step 5: 提交任务变更**

  ```bash
  git add web/src/stores/user.js web/src/components/UserInfoComponent.vue web/src/components/SettingsModal.vue web/test/unit/userExperience.test.js
  git commit -m "fix: 收口 SaaS 用户资料和设置入口"
  ```

### Task 3: 退出登录竞态修复

**Files:**
- Modify: `web/src/stores/user.js`
- Modify: `web/src/components/UserInfoComponent.vue`
- Modify: `web/src/router/index.js` (only if guard needs an explicit no-token branch)
- Test: `web/test/unit/userExperience.test.js`

**Interfaces:**
- Consumes: 当前 token、`/api/auth/logout`、Pinia agent store reset。
- Produces: `logout()` 立即清理本地身份，异步注销后端，调用方可等待后使用 `router.replace('/login')`。

- [ ] **Step 1: 写失败测试**

  ```js
  test('logout clears token before a slow server response resolves', async () => {
    const deferred = createDeferred()
    const store = createUserStoreWithLogoutPromise(deferred.promise)
    const pending = store.logout()
    assert.equal(store.isLoggedIn, false)
    deferred.resolve({ ok: true })
    await pending
  })
  ```

- [ ] **Step 2: 运行测试确认失败**

  Run: `cd web && pnpm test:unit --test-name-pattern="logout clears token"`

  Expected: FAIL，因为当前 `logout` 在网络请求完成后才清 token。

- [ ] **Step 3: 实现最小改动**

  保存旧 token 后立即清空 store、本地存储并 reset agent store；用保存的 token 发送后端注销请求；UserInfo 使用 `router.replace('/login')`，并等待 `logout()` 完成但不让后端失败阻塞跳转。

- [ ] **Step 4: 运行测试确认通过**

  Run: `cd web && pnpm test:unit --test-name-pattern="logout clears token"`

  Expected: PASS；手动验证从受保护页面退出后浏览器地址为 `/login`。

- [ ] **Step 5: 提交任务变更**

  ```bash
  git add web/src/stores/user.js web/src/components/UserInfoComponent.vue web/src/router/index.js web/test/unit/userExperience.test.js
  git commit -m "fix: 修复退出登录跳转竞态"
  ```

### Task 4: NewAPI 登录同步后的模型缓存刷新

**Files:**
- Modify: `backend/server/routers/auth_router.py`
- Modify: `backend/package/yuxi/models/providers/cache.py` (only if existing rebuild cannot refresh shared state)
- Test: `backend/test/unit/server/test_auth_router.py` or `backend/test/unit/services/test_model_cache.py`
- Test: `backend/test/integration/api/test_auth_router.py` (extend login path if fixture supports SaaS)

**Interfaces:**
- Consumes: `_upsert_saas_model_provider`, `pg_manager`, `model_cache.rebuild`。
- Produces: 登录同步提交后调用现有模型缓存重建，且远端拉取失败保留已有 `enabled_models`。

- [ ] **Step 1: 写失败测试**

  添加单元测试，mock NewAPI 返回新模型、数据库提交和 `model_cache.rebuild`，断言登录同步后 rebuild 被调用；再添加失败拉取场景断言旧模型仍在 provider payload 中。

  ```python
  async def test_saas_model_sync_rebuilds_cache_after_commit(monkeypatch):
      rebuild = Mock()
      monkeypatch.setattr(model_cache, "rebuild", rebuild)
      await _upsert_saas_model_provider(db, "key", "https://newapi.example")
      await db.commit()
      assert rebuild.called
  ```

- [ ] **Step 2: 运行测试确认失败**

  Run: `docker compose exec -T api uv run pytest backend/test/unit/server/test_auth_router.py -k "model_sync" -q`

  Expected: FAIL，因为当前登录同步只写数据库，没有在同一流程重建缓存。

- [ ] **Step 3: 实现最小后端改动**

  在 SaaS 登录同步成功并提交事务后调用现有 `_refresh_model_cache` 或等价的 `model_cache.rebuild`；将 `_upsert_saas_model_provider` 的异常路径改为不覆盖已有模型列表；避免在每次页面请求中增加远端请求。

- [ ] **Step 4: 运行测试确认通过**

  Run: `docker compose exec -T api uv run pytest backend/test/unit/server/test_auth_router.py backend/test/unit/services/test_model_cache.py -q`

  Expected: PASS；再运行登录集成测试确认 token 和 SaaS 会话行为不变。

- [ ] **Step 5: 提交任务变更**

  ```bash
  git add backend/server/routers/auth_router.py backend/package/yuxi/models/providers/cache.py backend/test/unit/server/test_auth_router.py backend/test/unit/services/test_model_cache.py backend/test/integration/api/test_auth_router.py
  git commit -m "fix: 登录后刷新 SaaS 模型缓存"
  ```

### Task 5: 租户知识库列表、目录和文档权限闭环

**Files:**
- Modify: `backend/server/routers/knowledge_router.py`
- Modify: `backend/server/utils/employee_kb_permissions.py`
- Modify: `backend/package/yuxi/knowledge/manager.py`
- Modify: `backend/server/utils/knowledge_response.py`
- Modify: `web/src/stores/database.js`
- Modify: `web/src/views/DataBaseView.vue`
- Modify: `web/src/views/DataBaseInfoView.vue`
- Modify: `web/src/router/index.js`
- Modify: `web/src/apis/knowledge_api.js` (only for accessible folder/document calls if missing)
- Test: `backend/test/integration/api/test_knowledge_router_employee.py`
- Test: `backend/test/integration/api/test_tenant_knowledge_router.py`
- Test: `backend/test/unit/services/test_knowledge_folder_access.py`
- Test: `web/test/unit/database_store.test.js`

**Interfaces:**
- Consumes: `get_saas_employee_context`, `get_databases_by_uid`, `share_config` permission resolution, existing folder/document endpoints。
- Produces: 员工只看到本租户且命中 `read_scope` 的知识库；返回 `can_read`/`can_manage` 等有效权限；`manage_scope` 员工可文档操作但永远不能 CRUD 知识库或目录。

- [ ] **Step 1: 写失败测试**

  后端集成测试覆盖三类身份：

  ```python
  async def test_employee_lists_tenant_databases_and_folders(client, employee, tenant_database):
      response = await client.get("/api/knowledge/databases/accessible", headers=employee.headers)
      assert response.status_code == 200
      assert [item["kb_id"] for item in response.json()["databases"]] == [tenant_database.kb_id]

  async def test_employee_cannot_create_folder_or_database(client, employee, tenant_database):
      folder_response = await client.post(f"/api/knowledge/databases/{tenant_database.kb_id}/folders", headers=employee.headers, json={"folder_name": "x"})
      assert folder_response.status_code in {403, 404}
  ```

  增加 `read_scope` 允许但 `manage_scope` 不允许上传/删除文档，以及跨租户返回 404 的断言；前端 store 测试断言 SaaS 员工调用 accessible 接口并不调用管理员列表接口。

- [ ] **Step 2: 运行测试确认失败**

  Run: `docker compose exec -T api uv run pytest backend/test/integration/api/test_knowledge_router_employee.py backend/test/unit/services/test_knowledge_folder_access.py -q` and `cd web && pnpm test:unit --test-name-pattern="database"`

  Expected: FAIL，至少体现当前角色/路由守卫导致的管理员入口或目录权限不符合设计。

- [ ] **Step 3: 实现最小后端改动**

  - 确保 accessible 列表按 SaaS `tenant_id`、`read_scope` 返回知识库，并序列化目录/详情所需字段；
  - 所有目录 CRUD 继续要求管理员角色；
  - 文档写操作走现有 `manage_scope` 依赖，读取操作走 `read_scope`；
  - 统一跨租户资源 404，权限不足 403；
  - 不允许通过前端隐藏绕过后端授权。

- [ ] **Step 4: 实现最小前端改动**

  `databaseStore.loadDatabases()` 对 `saasMode` 强制使用 accessible 接口；DataBaseView 隐藏新建知识库；详情页根据 `can_manage` 和员工身份隐藏知识库/目录 CRUD，仅保留命中 `manage_scope` 时的文档操作；移除或调整仅 `requiresAdmin` 的知识库详情守卫，让后端权限决定结果。

- [ ] **Step 5: 运行测试确认通过**

  Run: `docker compose exec -T api uv run pytest backend/test/integration/api/test_knowledge_router_employee.py backend/test/integration/api/test_tenant_knowledge_router.py backend/test/unit/services/test_knowledge_folder_access.py -q` and `cd web && pnpm test:unit --test-name-pattern="database"`

  Expected: PASS；验证本地管理员知识库创建、目录操作和文档操作不回归。

- [ ] **Step 6: 提交任务变更**

  ```bash
  git add backend/server/routers/knowledge_router.py backend/server/utils/employee_kb_permissions.py backend/package/yuxi/knowledge/manager.py backend/server/utils/knowledge_response.py web/src/stores/database.js web/src/views/DataBaseView.vue web/src/views/DataBaseInfoView.vue web/src/router/index.js web/src/apis/knowledge_api.js backend/test/integration/api/test_knowledge_router_employee.py backend/test/integration/api/test_tenant_knowledge_router.py backend/test/unit/services/test_knowledge_folder_access.py web/test/unit/database_store.test.js
  git commit -m "fix: 适配租户知识库和员工文档权限"
  ```

### Task 6: 文档、变更日志与全量验证

**Files:**
- Modify: `docs/develop-guides/changelog.md`
- Check: `docs/.vitepress/config.mts` only if a new formal user-facing document was added (not expected)

- [ ] **Step 1: 更新变更日志**

  在现有未提交的同类条目附近追加本次六项行为修复，写清 SaaS 资料/设置、退出、模型缓存和租户知识库权限边界。

- [ ] **Step 2: 运行前端测试和构建**

  ```bash
  docker compose exec -T web pnpm test:unit
  docker compose exec -T web pnpm build
  ```

  Expected: 全部通过，无 Vue 编译错误。

- [ ] **Step 3: 运行后端格式、Lint 和相关测试**

  ```bash
  docker compose exec -T api uv run ruff format --check package server test
  docker compose exec -T api uv run ruff check package server test
  docker compose exec -T api uv run pytest backend/test/unit backend/test/integration/api/test_auth_router.py backend/test/integration/api/test_knowledge_router_employee.py backend/test/integration/api/test_tenant_knowledge_router.py -q
  ```

  Expected: 相关测试通过，Lint 无新增错误。

- [ ] **Step 4: 手工验收关键路径**

  在 Docker 服务中验证：Agent 执行态收起、SaaS 资料摘要、设置入口、退出跳转、NewAPI 重新登录模型列表、租户知识库/目录以及 `manage_scope` 文档上传。

- [ ] **Step 5: 检查差异和工作区**

  Run: `git diff --check` and `git status --short`

  Expected: 无空白错误；只保留本次修改及用户原有未提交文件，不误删或覆盖后者。

- [ ] **Step 6: 提交文档与最终改动**

  ```bash
  git add docs/develop-guides/changelog.md
  git commit -m "docs: 更新 SaaS Agent 体验适配变更记录"
  ```
