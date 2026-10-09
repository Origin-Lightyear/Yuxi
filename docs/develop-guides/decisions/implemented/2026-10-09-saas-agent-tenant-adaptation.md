# SaaS Agent 体验与租户能力适配

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/saas_identity.py

## 问题

SaaS 分支引入员工身份、租户隔离、企业数据工具与登录同步，与远端已收敛的 thin-router、FIFO Request/Run、统一推理展示和 Yuxi 品牌同时演进。两套分支在权限判定位置、知识库字段与脱敏、Run 派发入口、Skill/MCP 依赖声明和命名上直接冲突；需要在不破坏远端架构不变量的前提下合入 SaaS 能力，并明确每一项主张的语义 Owner。

## 决策

### 实现方案

- 身份与权限：`saas_identity.py` 提供员工上下文（租户与部门）；`is_employee` 以 role 存在且非 admin/superadmin 为真，缺 role 按平台用户处理，管理门禁沿用 `getattr(current_user, "role", None)` 缺省跳过约定。登录同步由 `saas_auth_service.py` 拥有（模型供应方、saas-mcp 服务器与员工用户 upsert）。知识库员工门禁保留在 knowledge_router 的 in-body 结构（`require_kb_manage_for_documents` / `require_documents_edit_or_kb_manage`），与远端 `_ensure_database_supports_documents` 双结构并存；负向案例由 `test/integration/api/test_knowledge_router_employee.py` 拥有，直调式 unit 用 autouse 桩隔离。
- 知识库持久化与脱敏：`KnowledgeBaseSummary` 与 `knowledge/manager.py` 增加 `tenant_id`、`uploader_id`；`get_databases_by_user` 先按租户过滤再解析权限。敏感参数脱敏为并集规则：平台管理员且拥有管理权时保留明文，只读视图与非管理员（含 manage_scope 命中的员工）一律脱敏。
- 沙盒下载：`upload_kb_file` 只经 `_runtime_sandbox_scope` 与 `ProvisionerSandboxBackend.download_authorized_file_to_path` 在 owning filesystem boundary 内完成校验与读取，异常分类映射为用户可读错误，临时文件保留虚拟路径 basename 供 `upload_employee_document` 命名。
- Run 派发：`schedule_runner` 通过 `submit_agent_request` 提交，定时入口与普通请求一致——先持久化 Message 与 AgentRunRequest，仅 ready 队头创建 AgentRun；不再存在 `create_agent_run_view` 直建入口。
- 模型回写：`chat_service` 在 `config.is_agent_data_sync_enabled` 开关下把最终回复 fail-soft 双写 Tenant 服务（`agent_data_sync.py`），同步失败不改变对话结果。
- Skill/MCP：mysql-reporter frontmatter 声明 `mcp_dependencies: ["saas-mcp", "mcp-server-chart"]` 并保留 `linko_ent` 外部工具契约；显式 MCP 由 Graph 基础工具注册，`SkillsMiddleware._get_mcp_tools_from_context` 只加载 Skill 新增依赖并透传 uid，依赖缺失时按 unavailable 跳过并告警。
- 合流与命名：主分支合流结果为远端 main 加单个聚合提交，界面、文档与 CI 品牌跟随远端 Yuxi；丢弃本地 Yuxi → Linko AI 重品牌，`linko_ent`、`saas-mcp`、`mcp-server-chart` 等外部契约名例外保留。

## 替代方案

- keep：沿用本地全量 SaaS 版路由与服务形态，门禁继续放在 router 内；与 thin-router 契约和远端测试直接冲突。
- narrow：只保留远端结构、丢弃 SaaS 功能；失去租户能力与验收目标。
- replace：重品牌跟随本地 Linko AI；与远端文档、快照、CI 全面冲突且不在验收范围内。
- remove：删除 SaaS 员工门禁，仅保留远端 `_ensure_database_supports_documents`；员工可见性与编辑权失去负向案例覆盖。
- accepted：远端结构为骨架、SaaS 功能为增量，双门禁结构并存并以并集脱敏规则合并双方安全主张。

## 后果

- 权限最终仍在后端依赖与 repository 可见性查询处执行；前端 `canManageKnowledge` 等守卫只影响 UI，不是授权边界。
- 缺 role 的历史用户按平台用户处理，不触发员工门禁；租户过滤只对携带 `tenant_id` 的会话生效。
- 脱敏规则对平台管理员只读视图比本地更严格（保留远端安全主张），对 manage_scope 员工比远端更严格。
- `mcp-server-chart` 属 retired builtin slug，Skill 运行时依赖缺失按跳过处理，不阻塞技能激活。
- integration/E2E 需要真实 PostgreSQL 与 docker 拓扑；本机无 docker 时未执行，属已知未验证范围。

## 验证

- `python3 scripts/verify_engineering_contracts.py`：通过（48 decisions / 5 workflows / 4 agents files / 187 docs / 27 routers / 252 web sources）；`python3 -m unittest scripts.test_verify_engineering_contracts`：63 项 OK。
- `uv run --group test pytest test/unit -m "not slow"`：2603 passed。覆盖 is_employee 边界（缺 role → 非员工）、knowledge router 清理 21 项、KB 文档工具 6 项与沙盒缺文件负向（越界/超限负向在 sandbox backends 单测）、MCP HTTP 异常透传 47 项、file listing 租户字段与脱敏、builtin discovery 依赖声明与 skills middleware uid 透传。
- `corepack pnpm run test:unit`：410 passed；`corepack pnpm run lint:check`：通过。覆盖模型绑定 metadata 回退、脱敏后的知识库详情菜单、设置面板惰性挂载与 API Keys 可编辑，以及 database store 身份变化 watcher。
- `uv run ruff check package`、`uv run ruff format package --check`、`uv run ruff check --select I package`：与 CI（ruff.yml）一致，全部通过；`git diff --check` 通过。
- 未执行：真实 HTTP integration、worker/队列与浏览器 E2E（本机 docker 不可用）。

旧能力不存在：`upload_kb_file` 自有虚拟路径解析（`resolve_virtual_path` / `sandbox.paths`）、`create_agent_run_view` 直建入口、本地重品牌字样与旧推理展示孤儿（`resolveReasoningPresentation`）均已删除，符号搜索无残留；`linko_ent` 外部契约名保留。

重新引入条件：仅当出现必须绕过 owning filesystem boundary 读取宿主路径的真实工具消费方时，才重新讨论 `upload_kb_file` 的下载边界；仅当远端 thin-router 或 FIFO 契约被显式修订时，才允许 SaaS 门禁从 in-body 结构迁移到其他层。
