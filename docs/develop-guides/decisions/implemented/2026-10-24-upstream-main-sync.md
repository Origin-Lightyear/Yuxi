# 上游 main 同步（2026-10-21）

状态：implemented
类型：architecture
Owner：ARCHITECTURE.md

跨 Owner 分工：`ARCHITECTURE.md` 拥有系统边界与主链路描述；`docker-compose.yml` / `docker-compose.prod.yml` 拥有服务拓扑与运行前提；`backend/package/yuxi/storage/postgres/manager.py` 拥有 Schema 版本门控；`backend/package/yuxi/services/agent_request_service.py` 拥有请求提交与消息双写边界；`backend/package/yuxi/services/run_worker.py` 拥有 Run 执行与空闲看门狗；`packages/yuxi-im-gateway/` 拥有 IM 网关运行时。

## 问题

本地 fork 的最后一次上游快照是 2026-08-07（`e3897a19`）。此后上游推进了 364 个提交（Schema business 2→7、knowledge 1→2，移除 LITE 模式，替换 Redis 运行时配置同步等），本地在 IM 机器人/网关、SaaS 租户适配、桌面壳与运行可靠性上有 68 个提交。

两边历史被整体重写过，同一份快照的提交 SHA 不同，共同祖先退回到 2024-07-14。直接 `git merge origin/main` 会产生 564 个冲突，其中 552 个是 add/add，无法逐块判断保留哪一侧。

## 决策

### 实现方案

以内容一致的 8/7 快照 `e3897a19` 作为三方合并基准，用 `git merge-tree --write-tree --merge-base=e3897a19 origin/main main` 生成合并树，把冲突收敛到两边真正改过的 114 个文件、282 个冲突块，按模块分批解决。结果提交为双亲合并提交（`origin/main` + 本地 `main`），后续可直接 `git merge origin/main`。

冲突解决遵循三条规则：

- 上游结构为基线：模块划分、配置体系（PostgreSQL `ConfigOption`）、Schema 版本门控、请求/Run 状态机按上游实现落地。
- 本地行为重新接线到新结构：模型失效回退、USER 消息双写、租户知识库权限、IM 机器人路由与网关、MCP 超时、Run 空闲看门狗、桌面无边框壳。
- 上游已替代的本地表面直接删除，避免两套机制并存：

| 删除项 | 原因 |
|---|---|
| `backend/package/yuxi/agents/models.py` | 实现已迁入 `models/chat.py`，文件无引用且引用不存在的 `sys_config` |
| `backend/test/unit/config/test_runtime_config_sync.py` | 上游用 PostgreSQL `ConfigOption` 取代 Redis 运行时配置同步 |
| LITE 模式路由裁剪 | 上游移除 LITE，部署统一包含知识库、图谱与评估 |
| `tasker.shutdown()` 调用 | 上游改为分组件关闭 |

品牌修正：本地 sed 曾把 `YuxiClient` 改成含空格的 `Linko AIClient`、把 `X-Yuxi-Preview-*` 响应头改出空格、把文档与仓库链接改成带空格的 URL，本次全部恢复为代码标识与环境前缀，用户可见文案仍为 Linko AI。CLI 网页聊天令牌头 `X-Linko AI-Chat-Token` 含空格，HTTP 头名不合法，改为 `X-Yuxi-Chat-Token`。

测试基线调整（fork 行为导致，均在本记录同时生效）：

| 用例 | 调整 | 依据 |
|---|---|---|
| 设置页懒加载 | 移除 API Keys / 用户管理标签断言 | fork 用 `v-if="false"` 隐藏这两个入口 |
| 数据库创建流程 | 类型标签预期改为 Linko AI | 品牌文案 |
| 内置技能依赖 | 追加 `linko-enterprise` 与 `finance-mcp` | fork 内置技能 |
| 模型解析 | 用「首个可用聊天模型回退」与「无可用模型时 422」两条用例替换上游单条拒绝用例 | fork 的模型失效回退行为 |
| Run 空闲看门狗 | 测试桩补 `worker_id`、`aclose()`、`mark_run_terminal` 关键字参数 | 上游新增的 Run 上下文与关闭协议 |

## 替代方案

- **直接 `git merge origin/main`**：共同祖先退到 2024-07-14，552 个 add/add 冲突只能整体选边，会丢掉本地或上游的一侧实现，否决。
- **以本地为基线 rebase 到上游**：需要重放 68 个本地提交，冲突数量与合并接近，且改写已推送的 fork 历史，否决。
- **逐个 cherry-pick 上游 364 个提交**：历史清晰但工作量与冲突面最大，本地中间提交依赖旧结构，大量提交无法单独通过测试，否决。
- **只合并部分上游模块**：Schema 与配置体系强耦合，部分同步会让 API 与 worker 处于不兼容版本，否决。

## 后果

- 本地 `main` 与上游共享同一棵树的结构约定，下一次上游同步可以使用普通 merge。
- 运行时必须按 0.7.3 重建镜像并迁移数据库（business 10 / knowledge 2），旧镜像与新代码不兼容；`.env` 需要补齐 `API_KEY_DERIVATION_SECRET`。
- fork 的隐藏入口（API Keys、用户管理）与上游新设置页并存，后续需要决定是恢复入口还是删除后端能力。
- 上游 v0.7.3 的 `storage-migration` 在「未版本化 0.7.1 基线」上存在执行顺序缺陷：`rewrite_v071_workdir_paths` 在 `ensure_business_schema` 之前用当前 ORM 读取 Conversation，而 0.7.1 的 conversations 缺少 `creation_request_id`、`last_viewed_run_id`。从旧库升级需要先补这两列（`VARCHAR(64)`，可空）再运行 `scripts/migrate-storage.sh`；缺陷修复前不要直接用 `docker compose up` 触发迁移。
- 租户知识库路由原先在创建后调用已移除的 `agent_manager.reload_all()`；上游改为每次运行按数据库读取配置，本次删除该调用，租户创建链路恢复。
- 遗留风险：fork 的 SaaS 登录路径（`_saas_login`）没有调用 `record_login_failure`，SaaS 模式下「IP+账号」登录限流不会触发；该缺陷在合并前已存在，本次未修改，需要单独修复。
- 上游测试与 fork 行为的主要差异集中在上表五处，另有集成测试的环境适配：SaaS 双写会开启事务需在单测中隔离、fork 的 `im_bot_configs` 外键要求测试显式移除依赖约束、Project 生命周期 fixture 需要先落 User 再清理 ConversationStats。后续升级时需要重新核对。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 合并后无冲突残留、无空白错误 | 冲突标记进入源码 | 仓库树 | `grep -rn '^<<<<<<< \|^>>>>>>> '` 结果为 0；`git diff --check` 无输出 | 任一标记残留即失败 | Passed |
| 工程契约与文档、路由、前端源码一致 | 文档导航、路由边界、prose 规则破坏 | `scripts/verify_engineering_contracts.py` | `python3 scripts/verify_engineering_contracts.py`（53 decisions / 5 workflows / 4 agents files / 202 docs / 29 routers / 264 web sources）与 `python3 -m unittest scripts.test_verify_engineering_contracts` | 契约脚本非零退出 | Passed |
| 后端单元测试通过 | 合并语义破坏服务与运行时 | `backend/test/unit` | `uv run --group test pytest test/unit -m "not slow"`：2660 passed / 58 skipped | 任一用例失败 | Passed |
| 前端 lint、单测与构建通过 | 合并破坏组件与依赖 | `web/` | `pnpm lint:check`、`pnpm test:unit`（462 passed）、`pnpm build` | lint 错误或构建失败 | Passed |
| 桌面壳可运行 | 无边框改动丢失 | `desktop/` | `node --test`：7 passed | 用例失败 | Passed |
| 运行时升级与数据迁移 | 旧库启动新代码失败或丢数据 | `backend/package/yuxi/storage_migration.py` | `scripts/migrate-storage.sh` 完成 business 10 / knowledge 2；`/api/system/ready` 返回 ready；conversations 34、messages 446、agent_runs 83 与迁移前一致，34 个会话全部绑定 project | 迁移脚本非零退出即停止栈 | Passed（需先补两列，见后果） |
| 真实 HTTP 链路 | API、权限、持久化、队列 | `backend/test/integration` | `docker compose exec api uv run --no-sync --group test pytest test/integration -m "not slow"`：api 66 passed / 1 failed、services 126 passed / 1 failed / 1 error、graphs 3 passed、backends 1 skipped；204 个用例因缺少 `TEST_USERNAME` / `TEST_PASSWORD` 跳过 | 任一与本决策相关的用例失败 | 部分通过：租户知识库 9 个用例、项目生命周期 3 个用例、队列并发 8 个用例在修复后通过；剩余 3 个失败/错误均为下表的未验证项 |
| 未验证项 | 证据缺口 | — | 依赖管理员凭据的 204 个用例；`test_dataset_generation_resume_router.py` 在本机长任务挂起；`test_durable_task_worker_path.py` 的恢复用例与 `test_saas_identity.py` 在多事件循环连接池下报错；SaaS 登录限流按上文遗留风险未修复 | — | 未通过 |
