# Task 2 完成报告：SaaS 用户资料字段与设置入口

## 修改文件

- `web/src/stores/user.js`
  - 保持登录路径保存 `saas_mode`、`employee_code`。
  - 在初始化和 `getCurrentUser` 路径恢复 SaaS 身份字段。
  - 新增统一的 `formatUserSummary`，按“部门名 · 员工姓名”或仅姓名生成摘要。
- `web/src/components/UserInfoComponent.vue`
  - SaaS 员工显示部门与姓名摘要，隐藏本地角色文本。
- `web/src/components/SettingsModal.vue`
  - SaaS 身份仅保留账户设置，隐藏基本设置、OCR、沙盒环境变量和部门管理入口。
  - 本地管理员/超级管理员入口条件保持原权限逻辑。
- `web/src/components/AccountSettingsComponent.vue`
  - SaaS 账户资料隐藏本地权限角色项。
- `web/test/unit/userExperience.test.js`
  - 覆盖登录字段保存、当前用户刷新恢复和 SaaS 摘要格式。

## RED

```text
docker compose exec web node --test test/unit/userExperience.test.js
1 passed, 2 failed
```

失败原因分别为 `getCurrentUser` 未恢复 `saasMode`，以及缺少 `formatUserSummary`。

宿主机直接运行 `pnpm test:unit` 还会因工作区未安装 `pinia`/`vite` 在加载阶段失败，因此测试均在 `web` 容器中执行。

## GREEN

```text
docker compose exec web node --test test/unit/userExperience.test.js --test-name-pattern="saas identity|saas profile"
3 passed, 0 failed

docker compose exec web pnpm test:unit
20 passed, 0 failed

docker compose exec web pnpm exec eslint src/stores/user.js src/components/UserInfoComponent.vue src/components/SettingsModal.vue src/components/AccountSettingsComponent.vue
passed

docker compose exec web pnpm build
built successfully
```

`git diff --check` 通过。构建仍报告项目已有的超大 chunk 和依赖包 `#__PURE__` 注释警告，不影响构建结果。

## 提交

- Commit: `7699e7d8`（报告更新后 amend，最终 SHA 见交付消息）

## 风险

- 本次设置导航以 `saasMode` 为总开关；若未来 SaaS 管理员需要使用本地管理员配置入口，需要新增明确的权限/租户策略，而不能直接复用本地 `isAdmin`。
- 工作区存在其他代理或用户的未提交改动，本次提交仅包含上述 Task 2 文件和报告。

## 复审 Round 1 修复

### RED

新增 `backend/test/unit/routers/test_auth_router_cli_auth.py::test_auth_router_me_exposes_persisted_saas_identity`，通过真实 FastAPI `/api/auth/me` 路由请求带有 `UserConfig.tenant_id/employee_id` 的用户。修复前测试失败：响应缺少 `saas_mode`，抛出 `KeyError`。

### GREEN

- `UserResponse` 新增 `saas_mode` 和 `employee_code` 字段。
- `/api/auth/me` 使用现有 `get_saas_employee_context` 持久化身份判断 SaaS 模式，并以用户 UID 返回 employee code；本地用户返回 `false`/`null`。
- 部门管理内容补充 `!saasMode`，与导航入口一致。

验证结果：后端 auth router 单测 `2 passed`；前端 SaaS 测试 `3 passed`；前端全量单测 `20 passed`；ESLint 通过；构建成功。auth 集成测试因未配置 `TEST_USERNAME/TEST_PASSWORD` 跳过。构建保留已有依赖注释和 chunk 大小警告。

复审修复提交 SHA：`25d52019`。
