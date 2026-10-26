# 老板分身与经验引用约束

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/middlewares/founder_twin.py

## 问题

原型里的「老板分身」只是静态对话演示：员工可以问，但没有任何经验事实、引用约束或转交路径。直接把它做成一个普通 Agent，模型仍会用常识编造答案，管理者也无法确认哪些判断真正来自老板。

P3 需要把分身收敛成可验证的产品能力：经验条目有确认状态和来源；回答必须引用本分身已确认条目；没有依据时只能转交老板；转交单进入 P2 待办收件箱；普通员工只读咨询和自己的记录。

设计文档：`docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

## 决策

### 实现方案

入口是 `/founder-twin` 页面和 `founder_twin` Agent 会话。HTTP 层 `founder_twin_router.py` 负责身份依赖、请求校验和 DTO；`founder_twin_service.py` 负责经验状态、embedding、引用校验和转交记录；`work_item_repository.py` 负责把 Run 和转交单投影到统一待办。运行时由 `ChatbotAgent` 在 `context.agent_kind == founder_twin` 时装配 `FounderTwinMiddleware` 和 `FounderTwinAnswerGuardMiddleware`。失败边界是：无 embedding 时确认/检索 fail-closed；无成功提交或转交时 guard 第一次纠正、第二次让 Run 失败；转交可见性在 service 计算后传给 repository，越权详情统一 404。

### Agent 与权限

复用 `Agent.kind='founder_twin'`，不新建分身主表；`backend/package/yuxi/agents/digital_employee.py` 增加 `FOUNDER_TWIN_KIND` 与 `MANAGED_AGENT_KINDS`。`BaseContext.agent_kind` 由 `Agent.kind` 在 `load_agent_run_context` 写入，作为运行时是否启用分身工具和 guard 的唯一开关。

`/api/founder-twins` 只允许管理员创建；默认 `read_scope=global`、`manage_scope=None`，创建者通过现有 Agent 权限策略获得管理权。普通员工可以查看分身、已确认经验和自己的咨询/转交，不能创建、编辑、确认、失效经验，也不能回答转交。管理统计只返回给管理者；普通员工只看到已确认经验数，不暴露草稿数或待处理转交数。

### 数据与检索

新增三张表：

- `experience_entries`：`twin_slug + code` 唯一；状态 `draft -> confirmed -> invalid`；保存来源、确认人、引用计数、候选向量。
- `experience_consultations`：`run_id` 唯一；保存问题、答案、引用 ID、置信度和关联转交单。
- `handoff_requests`：`status` 只允许 `open/answered/closed`；保存问题、回答人、回答和时间。

不引入 pgvector。确认经验时用现有 `select_embedding_model` 生成 embedding，存 JSONB；查询时加载本分身最多 2,000 条 confirmed 向量，在进程内计算余弦 top-k。创建经验时执行 2,000 条上限；编辑任何已确认条目会退回草稿并清空旧向量；失效条目清空向量，避免被后续检索误用。确认失败时返回 503 且不写入 `confirmed`，不把未就绪伪装成成功。

### 工具与 guard

`FounderTwinMiddleware` 只在 `agent_kind=founder_twin` 时注册三个工具并追加系统提示：

- `retrieve_experience`：检索当前分身的已确认经验。
- `submit_consultation_answer`：引用不能为空；逐条校验条目属于当前分身、状态 confirmed 且有 embedding；成功后按 `run_id` 新建或更新唯一咨询记录，只对新增引用累加 `citation_count`。
- `create_handoff_request`：写 `open` 转交单，并把本次咨询记录指向它；`assigned_to_uid` 取分身创建者。

`FounderTwinAnswerGuardMiddleware` 在 `aafter_model` 检查当前用户轮次之后的工具消息。模型带 tool calls 时先放行等待工具执行；最终文本必须与成功 `submit_consultation_answer` 的 `answer` 归一化后一致，或已经成功 `create_handoff_request`。首次违规注入一条 `founder_twin_guard` 纠正消息并跳回模型；同一轮再次违规抛 `RuntimeError`，让 Run 显式失败。不能靠提示词或 HTTP 200 证明约束生效。

### 转交单接入待办

P2 的 repository 从只投影 Run 改为 `UNION ALL`：

- Run：仍取每线程最新顶层 Run，排除 subagent/agent_call/agent_evaluation。
- Handoff：`open -> running`、`answered -> completed`、`closed -> cancelled`；`approval_kind=None`。
- 统一 key：`run:<id>` / `handoff:<id>`；DTO 对转交返回 `source_type=handoff`、`thread_id=None`、`run_id=None`，发起人是提问员工，不是当前查看的管理者。
- 可见范围：转交单对发起人可见；对能管理该分身的用户也可见。管理范围在 service 中由 `Agent.kind='founder_twin'` 和 `user_can_manage_agent` 计算，再作为已授权 slug 列表传给 repository，避免在 SQL 中复制权限规则。
- summary、未读数和详情使用同一 union；排序和分页在 union 后执行，不能先分别分页再合并。

### 前端

新增 `/founder-twin` 页面：左侧分身列表，右侧经验库、咨询记录和转交 tab。管理员可创建分身、创建/编辑/确认/失效经验和回答/关闭转交；普通员工只看到已确认经验和自己的咨询/转交。`开始咨询` 复用 `AgentView` 的 `agent_id` 入口，不复制聊天链路。转交待办通过 P2 的 `workItemTarget` 打开 `FounderTwinComp`。

## 替代方案

- **新建 `founder_twins` 主表**：会把 Agent 的分享、权限、会话和运行时入口复制一遍。复用 `Agent.kind` 让分身直接拥有现有调用链路，代价是通用 Agent 接口必须显式拦截 P3 管理动作。
- **用 pgvector/Milvus**：当前单分身上限 2,000 条、查询低频，独立向量索引带来同步和运维成本；进程内余弦在规模内足够，超过上限时再迁移。
- **只靠提示词要求引用**：无法证明模型真的使用了经验；工具校验和 guard 才能在 Run 上产生拒绝后果。
- **允许无引用回答**：会把「老板分身」退化成普通模型；没有依据时转交是产品边界。
- **转交只给发起人看**：老板无法在收件箱回答；因此可见范围必须是发起人 + 分身管理者。
- **把 `answer_message_id` 作为当前验收条件**：当前引用审计以 `run_id` 和咨询记录为主键，消息 ID 不参与 UI 或引用校验；先保留列，后续在 Run 完成事务中回填，避免在本期复制输出持久化边界。

## 后果

- 分身可用性依赖 embedding 模型。当前本地环境 SiliconFlow 返回 401、Ollama 不可用，确认经验会失败关闭；真实部署必须先配置可用 embedding。没有模型时检索返回错误，模型只能转交，不会编造。
- 2,000 条上限和进程内余弦限制单分身规模和查询吞吐；超过后需要独立向量索引和迁移决策。
- guard 只检查当前 LangGraph 状态中最近用户轮次之后的工具消息；它不能证明模型内部「理解」了经验，只能证明最终答案与成功提交的引用答案一致。
- `citation_count` 是提交次数近似值；同一 Run 重复提交只对新增引用累加。若模型先成功提交答案后又转交，已累加计数不会回滚；正常 guard 流程下不会发生。
- `answer_message_id` 当前为空；咨询记录以 `run_id` 绑定 Run，消息级定位需要后续回填。
- 转交回答不会自动恢复原会话或通知提问人；当前通过待办状态和分身页面查看结果。
- 普通员工能看到管理者的公开分身说明和已确认经验，但看不到内部 prompt、embedding、草稿或全部转交。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| `founder_twin` 进入 Context，普通 Agent 不启用分身逻辑 | 所有 Agent 都被注入工具 | `load_agent_run_context`、`BaseContext.agent_kind`、`ChatbotAgent._build_middlewares` | `test_agent_run_context.py` 2 passed；guard 非 founder 直接放行 | 删除 `agent_kind` 赋值后 context 测试变红；删除条件后 guard 测试变红 | Passed |
| v11→v12 三表、唯一约束和外键幂等 | 旧库缺表或重复升级失败 | `models_business.py`、`manager.py`、`storage_migration.py` | `test_founder_twin_migration.py` 1 passed；schema 版本测试 13 passed | 删除 DDL/唯一约束/外键断言后迁移测试变红 | Passed |
| 管理员可创建、编辑、失效经验；普通员工只读 | 员工通过 API 改经验或看到草稿 | `founder_twin_router.py`、`founder_twin_service.py` | `test_founder_twin_api.py` 2 passed | 员工 POST entry/confirm 返回 403；普通入口只返回 confirmed | Passed |
| 确认失败不写 confirmed | embedding 失败被伪装成功 | `confirm_experience`、`confirm_entry` | `test_founder_twin_service.py::test_confirm_generates_embedding_and_failure_keeps_draft` | mock embed 抛错后状态仍 draft、embedding 仍 None | Passed |
| 引用必须非空、同分身、confirmed 且有向量 | 引用别的分身/失效条目也能通过 | `validate_citations`、submit 工具 | `test_founder_twin_tools.py` 4 passed：空引用、其他分身、invalid、不存在 ID 均失败 | 去掉 twin/status/embedding 条件后测试变红 | Passed |
| 同一 Run 重复提交只保留一条咨询 | 重复插入导致唯一约束错误或双计数 | `record_consultation_answer`、`run_id` 唯一 | tools 测试同一 run 提交两次，count=1，新增引用只加一次 | 删除 run_id 唯一或不做 upsert 后测试变红 | Passed |
| guard 首次纠正、第二次失败，成功提交/转交放行 | 模型直接回答无引用答案 | `FounderTwinAnswerGuardMiddleware` | `test_founder_twin_guard.py` 4 passed：无工具、答案不一致、答案一致、handoff | 删除 guard 后测试变红；第二次仍不抛错则测试变红 | Passed |
| 转交单进入待办且只对本人/管理者可见 | 其他员工看到或管理者看不到 | `WorkItemRepository._handoff_select`、`workbench_service._managed_twin_slugs` | repository 4 passed；workbench API 6 passed；43 个 P2/P3 相关测试 passed | 去掉 uid/managed 过滤后隔离测试变红；不加入 handoff select 时列表测试变红 | Passed |
| P2 页面和 P3 页面可构建 | 前端编译失败或 util 映射错误 | `FounderTwinView.vue`、`founderTwin.js`、`workItem.js` | `node --test`：484 passed；ESLint 0；`vite build` 成功 | 删除 handoff 目标的 FounderTwinComp 后 util 测试变红 | Passed |
| 全量后端 unit 无回归 | P3 改动破坏既有 unit | 全仓库 | `pytest test/unit -m "not slow"`：2701 passed, 58 skipped | 无 | Passed |

最低证据：真实 PostgreSQL v12 迁移；真实 HTTP 覆盖管理员/员工权限、分身列表、待办双源和转交可见性；agent guard/tool/graph unit；前端 unit/lint/build 全量通过。

未运行：
- 确定性 E2E：replay 网关 `http://localhost:8765/health` 不可达。
- 真实浏览器页面验证和截图：当前环境没有可用的登录会话和浏览器自动化；只完成 `vite build` 与前端全量测试。
- 真实 embedding 成功路径：本地 SiliconFlow 401、Ollama 不可用；成功 confirm 由 service unit mock 覆盖，live API 只验证了员工 confirm 403。
- 真实模型驱动的工具调用链路：需要可用 chat/embedding 模型与 deterministic replay；guard 和工具通过构造消息验证。

## 风险

- **embedding 是硬依赖**：未配置时经验无法确认、检索无法工作；上线前必须把模型可用性纳入 readiness 或明确展示不可用状态。
- **规模上限**：2,000 条和全量进程内余弦会随条目数线性变慢；超过后需要向量索引，且要重新设计受控引用。
- **guard 依赖工具消息格式**：如果模型返回的 ToolMessage 内容不是 JSON 或工具名变化，guard 会按失败处理；这是 fail-closed，但需要保持工具契约测试。
- **权限计算在 service 层**：`managed_twin_slugs` 每次待办查询都会扫描 founder_twin 列表；分身数量很大时需要索引或缓存，且必须保持 repository 不复制授权规则。
- **转交回答不等于闭环**：老板回答后不会自动重新运行提问人的原会话，也不会通知提问人；当前依赖待办已读和页面查看。
- **`answer_message_id` 未回填**：当前审计以 `run_id` 为主键；如果未来要求精确到消息，需要在 Run 输出持久化事务中同步更新，不能再开一条旁路写。
