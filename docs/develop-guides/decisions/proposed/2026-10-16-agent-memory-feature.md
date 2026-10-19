# 跨会话 (用户×Agent) 记忆条目

状态：proposed
类型：feature
Owner：backend/package/yuxi/agents/middlewares/agent_memory.py

跨 Owner 分工：`backend/package/yuxi/agents/middlewares/agent_memory.py` 拥有 Agent 记忆注入行为（每次模型调用前的记忆块拼装与查询失败降级）；`backend/package/yuxi/agents/middlewares/memory.py` 拥有既有 MEMORY.md 注入（本决策不改其行为）；`backend/package/yuxi/services/agent_run_manifest_service.py` 拥有 Run 身份扩展（`agent_slug` 进 context）；`backend/package/yuxi/services/run_worker.py` 拥有抽取任务的发布时机（仅 completed 终态、best-effort）；新建 durable task 模块拥有抽取逻辑与容量策略；`backend/package/yuxi/repositories/agent_memory_repository.py` 拥有条目持久化与 uid 强制过滤；`backend/server/routers/agent_memory_router.py` 拥有两个端点的薄路由；`web/src/views/AgentView.vue` 与新建 `AgentMemoryPanel` 组件拥有聊天页查看/删除交互。

## 问题

既有记忆只有 uid 维度的 `MEMORY.md` 文件方案（`memory_service` + `enable_memory` 开关），Agent 无法积累"对这个用户的了解"：换线程、换会话后对用户偏好一无所知，多 Agent 场景无法差异化对待用户。调研确认仓内无 Agent 维度记忆、无结构化记忆表；memory middleware 是既有注入 seam，但 `BaseContext` 缺 agent 身份，Run 链路中 `agent_slug` 只在 Run 行与 worker 局部可见。用户诉求：跨会话用户级记忆、云端持久化、按 (用户, Agent) 粒度、在 agent 上下文可查看与删除。

## 提案

### 实现方案

**与既有用户记忆并存**（`MEMORY.md` 方案原样保留），新增 (用户×Agent) 结构化条目作为第二记忆源注入同一条链路。

**数据模型**（仿 `scheduled_agent_jobs` 四层模式，无 Alembic，幂等 DDL 进 `ensure_business_schema`）

- 表 `agent_memories`：`id`、`uid`、`agent_slug`、`content`（text）、`source_run_id` + `extract_index`（同 Run 内序号，二者**复合唯一**，抽取幂等键）、`created_at`/`updated_at`；`uid` 与 `source_run_id` 不建外键，所有权由 repository 强制 `uid` 过滤保证，条目随 Run 生命周期由清理逻辑收敛；**硬删除**（隐私语义，不留 `deleted_at`）。
- 模型 `AgentMemory` + `to_dict()`；repository `agent_memory_repository.py`：`list_by_agent(uid, agent_slug)`、`delete(uid, agent_slug, memory_id)`、`add(...)`、`count_by_agent`——所有查询强制 `uid` 过滤。

**抽取管线**（Run 完成后 LLM 自动抽取，后台不阻塞派发）

- `task_registry` 注册 durable Task `memory_extract`（knowledge ingest 同款 lease/reconcile/重试）。
- `run_worker.process_agent_run` 的 finally 中，仅当 `final_run.status == completed` 时 `publish_task`；失败/中断的 Run 不抽取；发布失败只记日志，不改 Run 结果。
- 任务内读取该 Run 线程消息，用系统默认模型服务一次性抽取 0~N 条值得长期记住的用户事实/偏好（中文、每条 ≤100 字；无则空返回）；逐条写入（`extract_index` 0..N-1），`(source_run_id, extract_index)` 复合唯一约束兜底重试幂等。
- 容量：逐条写入后按 `created_at` 升序 trim 至 50 条（`delete_oldest_over_limit`），不做写入前预删。

**注入与开关**

- `prepare_run_execution` 身份注入块新增 `agent_slug`（`configurable=False`，值取自 Run 行，不信任外部输入）；`BaseContext` 声明该字段。
- 独立 `agent_memory.py` middleware（不改既有 memory middleware；因 `load_memory_prompt` 在 MEMORY.md 为空/缺失时返回 None，agent 记忆注入不能依赖 MEMORY.md 存在）：`enable_memory` 开启且 `context.agent_slug` 存在时，经 `memory_service` 只读函数按 (uid, agent_slug) 取最新 20 条（创建时间倒序），以 `<agent_memory>` 块并入同一次 `append_to_system_message`；**无条目不追加空块**；查询失败降级为不注入（不阻断运行）。middleware 不直查 SQL。子 Agent 图不挂该 middleware，作用域天然正确。
- `enable_memory` **统一管住新旧两套**：关闭时不注入两套、不发布抽取任务（源头不产生新条目）；已有条目保留可见可删。不开新开关。

**API 与 UI**

- 新 `agent_memory_router.py`（prefix `/api/agent`，注册进 `routers/__init__.py`）：
  - `GET /api/agent/{slug}/memories`：当前用户 × 该 agent 条目列表（`created_at` 倒序）；`get_visible_by_slug(user=...)` 不可见 404；登录即可。
  - `DELETE /api/agent/{slug}/memories/{memory_id}`：uid + agent_slug + id 三重过滤硬删，非本人条目 404（不泄露存在性）。
  - 不做增/改 API。
- 服务层 `agent_memory_service.py` 持有所有权校验；薄路由只做依赖注入。
- UI：`AgentView` 头部 agent 选择区旁加「记忆」图标按钮（可见该 agent 的用户均可点），打开 `AgentMemoryPanel`（Modal/Drawer）：条目列表（空态「暂无记忆」）、每条正文 + 创建时间 + 删除按钮（二次确认，删除就地刷新）；`enable_memory` 关闭时入口不出现。`web/src/apis/agent_memory_api.js` 仿 `scheduled_agent_api.js` 封装。admin 也只看得到自己的条目（无跨用户视图）。

## 替代方案

- 与 `MEMORY.md` 收编为单一存储：需迁移文件内容、改开关语义与读写全链路，范围膨胀而收益仅为"一种存储"，否决（用户已选并存）。
- Run finally 同步抽取：省 durable task 基建，但模型调用挂在 worker finally，慢/失败拖累派发与取消链路，违背仓内"后台重活走 durable task"分层，否决。
- Agent 工具主动写（`remember` 工具）+ 自动抽取双写：成本与质量不可预期叠加，留后续迭代，本期仅自动抽取。
- 记忆向量检索/语义去重：条目为 ≤100 字结构化短文本、上限 50 条，最新倒序注入已足够，否决。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| agent_memories 表幂等创建，重复升级不重建 | 升级丢条目或阻塞启动 | `storage/postgres/manager.py` | unit：`test_postgres_manager_schema.py` 断言 DDL 语句（CREATE TABLE IF NOT EXISTS、复合唯一索引先于重建、旧索引双名 DROP）；v9→v10 门控由 `test_storage_migration` 覆盖 | 语句二次执行幂等由 IF NOT EXISTS/ADD COLUMN IF NOT EXISTS 保证 | Inspected |
| completed Run 终态发布抽取任务且仅 completed 发布 | 失败/中断 Run 误抽取 | `services/run_worker.py` | unit：`test_run_worker_memory_publish.py` 5 用例直测 `_publish_memory_extract`（开关短路、model_spec 三段决议、不抛）；调用点位于既有 completed 分支经 code review 核对 | failed/cancelled 分支不触达 helper | Inspected |
| 抽取幂等：同 Run 重试不重复写条目 | 重试产生重复记忆 | durable task 模块 | unit：model/repo/handler 三层——同 `(source_run_id, extract_index)` 二次写入被唯一约束拒绝，`fail_add_at` 负向证明第 i 条冲突不影响其余 | 重复插入失败且已提交前序条目存活 | Passed |
| 容量 50 截断删最旧 | 超限无界增长 | durable task 模块 | unit：插入 51 条断言最旧被删；handler 断言 trim 收到常量 50 | 第 51 条后计数=50 且最旧消失 | Passed |
| 注入：有条目追加 `<agent_memory>`、无条目不追加空块 | 空块污染提示词 | `agents/middlewares/agent_memory.py` | unit：`test_agent_memory_middleware.py` 9 用例（含空 MEMORY.md 独立注入、查询失败降级返回 None、CancelledError 上抛） | 空列表不出现块标签 | Passed |
| enable_memory 关闭：不注入两套且不发布抽取 | 开关语义分裂 | memory middleware + run_worker | unit：注入侧 `create_agent_memory_middleware` 返回 None 且零 service 调用；发布侧开关关时 `options.get`/创建/发布全不触达 | 关闭后零注入零发布 | Passed |
| GET/DELETE 端点所有权：他人条目 404、不可见 agent 404 | 越权读删 | `agent_memory_router.py` + service | HTTP+PostgreSQL integration（`test/integration/api/test_agent_memory_api.py` 4 用例含独立会话回读，待 compose 环境执行） | 非本人 memory_id → 404 且行仍在 | Not run |
| agent_slug 身份来自 Run 行不信任外部输入 | 伪造 agent 身份读他人记忆 | `agent_run_manifest_service.py` | unit：`test_agent_slug_context.py`（`configurable_fields` 排除 + update_config 伪造被过滤）+ `test_agent_run_manifest_service.py` 断言 update 值即 `run.agent_slug` | 外部传入被忽略 | Passed |
| 前端面板：列表/空态/删除确认、开关关闭入口消失 | UI 不可用或误导 | `web/src/components/AgentMemoryPanel.vue` + `web/src/views/AgentView.vue` | web unit：`cd web && npm run test:unit`（AgentMemoryPanel 用例源码断言钉住 `:open` 门控表达式与 `v-if` enableMemory，全量 443 pass） | 空列表渲染空态文案；删除门控后断言在正确原因上失败 | Passed |
| 全量门禁通过（后端 unit、web unit/lint/build、contracts、docs build） | 回归破坏 | 各命令 Owner | `pytest test/unit -m "not slow"` 2694 passed；`npm run test:unit` 443/443；lint:check 0 警告；build 成功；contracts 51 decisions 通过；docs build 成功 | 正向命令 | Passed |

## 风险

- **抽取质量**：条目可能空泛或近重复——上限 50、每条 ≤100 字、用户可删兜底；prompt 调优留迭代。
- **成本增量**：每次 completed Run 一次额外小 prompt 调用；`enable_memory` 关闭即零成本。
- **生效延迟**：抽取为后台任务，条目下一次 Run 才生效，首条记忆非即时——接受。
- **上下文占用**：注入最新 20 条 × ≤100 字 ≈ 2KB 级，相对既有 system prompt 可忽略。
- **硬删除不可恢复**：符合隐私语义；误删需重新学习，无软删找回——接受。
- **抽取任务与 Run 派发竞争**：任务在 finally 后发布、经 queue 异步执行，不占用 Run lease；失败重试不影响派发链路（durable task 既有语义）。
- **v9 存量库升级**：`BUSINESS_SCHEMA_VERSION` 升到 10，使已记录 business=9 的库在升级时重新进入 `ensure_business_schema`，幂等补建 `agent_memories` 表并补齐索引；未升版本前 v9 库会因缺表导致记忆 API 500 与注入静默降级。
- **不可见线程的抽取任务**：subagent 子线程、invocation 来源与归档线程对普通用户不可见，`read_memory_messages` 抛 `ValueError` 时 handler 记为 `skipped` 而非 `failed`，避免产生失败任务与异常日志。
