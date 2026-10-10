# 财务工作台 S3：报表 Artifact 与独立查看页 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent 产出结构化报表 JSON 文件 artifact，聊天流内出现报表卡片，用户可在独立页查看指标/表格/图表、逐指标溯源并导出 CSV。

**Architecture:** 文件制报表（`{workdir}/outputs/reports/report-*.json`，固定 schema + `schema_version`），新内置工具 `write_report_artifact` 校验并落盘后以 `Command` 并入 state `artifacts`（与 `present_artifacts` 同一 reducer）；读取复用既有 `GET /api/chat/thread/{id}/artifacts/{path}`（线程归属校验即授权边界）；前端 `TOOL_RENDERERS` 注册工具卡 + `/report/:thread_id/:path(.*)*` 独立页（`a-table` + `dashboardCharts.js` 的 echarts + CSV Blob 导出）。

**Tech Stack:** Python（langchain tool、LangGraph Command）、FastAPI 既有 artifact API、Vue 3 + ant-design-vue 4 + echarts 6（按需）、node --test（前端）、pytest（后端）。

**Spec:** `docs/develop-guides/decisions/proposed/2026-10-10-finance-report-workspace.md`（已随 `f7408d5d` 提交；executor 必须先读）。

## Global Constraints

- 本机无 docker：后端测试用 host uv——worktree `backend/` 已 `uv sync --group test`；命令一律 `uv run --group test pytest ...`（cwd=backend）。integration/E2E 依赖服务栈，本机 Not run，只在最终报告记录，不得写成通过。
- 前端单测命令：web 根目录 `corepack pnpm run test:unit`；lint `corepack pnpm run lint`；构建 `corepack pnpm run build`。
- 提交前仓库根：`python3 scripts/verify_engineering_contracts.py` 必须通过。
- 报表 schema（spec 钉死，不得漂移）：`schema_version`(int=1)、`title`、`generated_at`、`summary`、`metrics[]`(`key/label/value/unit`+可选 `source`)、`tables[]`(`title/columns/rows`)、`charts[]`(`type∈{bar,line,pie}`+`xKey`+`series[]`(`name/dataKey`)+`data[]`)、`validation_status` 默认 `agent_declared`。
- 工具校验失败必须 fail-closed：返回错误 ToolMessage，**不落盘、不更新 artifacts**。
- 文件写入走 `ProvisionerSandboxBackend.upload_authorized_file_from_path`（参照 `ocr_parse_file` 的 tempfile + 线程安全模式）；路径由服务端生成，固定前缀 `{workdir_path}/outputs/reports/`，重名消解用 `-1` 追加（参照 `_next_ocr_output_path`）。
- `@tool(category="buildin", ...)` 装饰器注册；新模块必须被 `toolkits/buildin` 导入链 import 才会进注册表（见 Task 1 Step 3）。
- 每个新 guard 至少一个负向测试；commit 用中文 Conventional Commit + why body；只改验收所需范围。
- 全部新/改 `.py`、`.js` 文件中文 docstring（新增函数/类；改动沿用既有风格）。

## Review Focus

1. **前后端 `schema_version` 错位**：后端写入 `schema_version=2` 时前端当前只支持 1——独立页必须显示明确提示且不渲染主体、不白屏。测试归 Task 3（`parseReportArtifact` 超版本单测）+ Task 5（页面走该 helper 的失败分支）。
2. **CSV 导出的转义与编码**：单元格含逗号、双引号、换行、中文时导出必须 RFC4180 正确且带 UTF-8 BOM（Excel 打开不乱码）。测试归 Task 3 `buildTablesCsv` 负向样例。
3. **state `artifacts` 合并语义**：`write_report_artifact` 返回的 `Command(update={"artifacts": [path]})` 依赖既有 reducer 合并，不得整体覆盖已展示交付物。测试归 Task 1（断言 update 载荷形态）+ 既有 `test_agent_artifacts_state.py` 全绿。
4. **落盘路径逃逸与越权**：路径必须始终落在 `{workdir}/outputs/reports/` 下；`title` 等任何模型输入不得参与路径拼接（时间戳 + 消解后缀是唯一变量）。测试归 Task 1（断言生成路径前缀）。
5. **超大报表拖垮页面**：S3 不做分页/虚拟滚动（非目标），靠 skill 指令限制示例规模——Task 2 落指令；最终报告记为已知限制。

---

### Task 1: 后端 `write_report_artifact` 工具

**Files:**
- Create: `backend/package/yuxi/agents/toolkits/buildin/report_artifact.py`
- Modify: `backend/package/yuxi/agents/toolkits/buildin/__init__.py`（追加 `from . import report_artifact  # noqa: F401`，使装饰器注册生效）
- Test: `backend/test/unit/agents/toolkits/buildin/test_write_report_artifact.py`

**Interfaces:**
- Consumes: `_resolve_runtime_sandbox_scope(runtime) -> tuple[str, str, str]`（tools.py 内既有，返回 runtime_scope_id/uid/workdir_relative_path）、`_runtime_scope_value(runtime, "workdir_path")`（用于 `{workdir_path}/outputs/...` 相对路径前缀）、`ProvisionerSandboxBackend(thread_id=, uid=, workdir_path=, create_if_missing=True)`、`backend.upload_authorized_file_from_path(dest_rel_path, local_temp_path)`、`backend.regular_file_exists(path)`（消解重名）。
- Produces: 工具对象 `write_report_artifact`（langchain tool，装饰器自动进注册表）；`func(report: str | dict, runtime: ToolRuntime, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command`；`update` 至少含 `artifacts: [f"{workdir_path}/outputs/reports/report-<ts>[ -N].json"]` 与一条成功 ToolMessage。前端 Task 4/5 依赖返回路径在 `artifacts` 中可被 `present_artifacts` 同 reducer 合并。

- [ ] **Step 1: 写失败测试** `test_write_report_artifact.py`，全部用假 runtime（`types.SimpleNamespace(config={"configurable": {...}})`，`configurable` 至少含 `runtime_scope_id="t1"`、`uid="u1"`、`workdir_relative_path="u1/project"`、`workdir_path="u1/project"`）与 monkeypatch 的假 `ProvisionerSandboxBackend`（实例捕获 `upload_authorized_file_from_path` 调用与写入字节；`regular_file_exists` 返回可控集合）。用例：
  1. `test_writes_report_and_updates_artifacts`：最小合法报表（schema_version=1、title/generated_at/summary、metrics 一条含 source、tables 一条、charts 一条 bar）→ 断言 Command.update["artifacts"] == ["u1/project/outputs/reports/report-<非空时间戳>.json"]（用正则 `^u1/project/outputs/reports/report-\d{8}T\d{6}Z\.json$`）、成功 ToolMessage 含路径；上传的文件 `json.loads` 后等于输入且 `schema_version==1`、`validation_status=="agent_declared"`。
  2. `test_missing_required_field_fails_closed`：缺 `title` → 返回值是 Command，update 无 "artifacts" 键，messages[0] 为 ToolMessage 且 content 以 `Error:` 开头；假 backend 零上传。
  3. `test_unsupported_schema_version_fails_closed`：`schema_version=2` → 同上零上传。
  4. `test_invalid_chart_type_fails_closed`：charts[0].type="scatter" → 同上零上传。
  5. `test_report_json_string_accepted`：`report=json.dumps(合法报表)` 与 dict 等价通过（LLM 传字符串是主路径）。
  6. `test_collision_appends_numeric_suffix`：假 backend `regular_file_exists` 对第一个候选返回 True → 断言最终路径以 `-1.json` 结尾。
- [ ] **Step 2: 跑测确认 RED**
  Run: `uv run --group test pytest test/unit/agents/toolkits/buildin/test_write_report_artifact.py -q`（cwd=backend）
  Expected: FAIL（ModuleNotFoundError: report_artifact 或 ImportError）
- [ ] **Step 3: 实现 `report_artifact.py`**
  - `SUPPORTED_REPORT_SCHEMA_VERSION = 1`；`_REPORT_OUTPUT_DIR_NAME = "reports"`。
  - 校验函数 `_validate_report_payload(report) -> dict`：str 则 `json.loads`；缺必填/类型不符抛 `ValueError(中文说明)`；`charts[].type` 限 bar/line/pie；`schema_version` 必须是 int 且 `<= SUPPORTED_REPORT_SCHEMA_VERSION`；通过后原样返回（不丢弃未知键，向前兼容）。
  - `_next_report_path(backend, workdir_path) -> str`：`f"{workdir_path}/outputs/reports/report-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"`，存在则 `-1`、`-2`…（参照 `_next_ocr_output_path`）。
  - 工具函数：先 `_validate_report_payload`（失败 return `Command(update={"messages": [ToolMessage(content=f"Error: {exc}", tool_call_id=tool_call_id)]})`）；再 `_resolve_runtime_sandbox_scope(runtime)` 取 scope，构造 `ProvisionerSandboxBackend(..., create_if_missing=True)`；tempfile 写 UTF-8 JSON（`json.dumps(payload, ensure_ascii=False, indent=2)`）后 `await asyncio.to_thread(backend.upload_authorized_file_from_path, rel_path, temp)`；finally 删 tempfile。成功 return `Command(update={"artifacts": [rel_path], "messages": [ToolMessage(content=f"报表已写入 {rel_path}（schema_version={payload['schema_version']}）", tool_call_id=tool_call_id)]})`。
  - 装饰器：`@tool(category="buildin", tags=["报表", "交付物"], display_name="写入报表", description=...)`，description 用模块常量 `WRITE_REPORT_ARTIFACT_DESCRIPTION`（中文，说明 schema 必填字段与 fail-closed 行为）。工具参数单个 `report`（str，接受 JSON 字符串或对象）。
  - `buildin/__init__.py` 追加导入。
- [ ] **Step 4: 跑测确认 GREEN**
  Run: `uv run --group test pytest test/unit/agents/toolkits/buildin/test_write_report_artifact.py test/unit/services/test_agent_artifacts_state.py -q`
  Expected: 全部 PASS（含既有 artifacts state 测试无回归）
- [ ] **Step 5: 顺带验证注册表**
  Run: `uv run --group test pytest test/unit/agents -q`
  Expected: PASS（确认新装饰器注册未破坏既有工具收集）
- [ ] **Step 6: Commit**
```bash
git add backend/package/yuxi/agents/toolkits/buildin/report_artifact.py backend/package/yuxi/agents/toolkits/buildin/__init__.py backend/test/unit/agents/toolkits/buildin/test_write_report_artifact.py
git commit -m "feat(toolkits): 新增 write_report_artifact 报表写入工具" -m "S3 验收：校验 fail-closed 不落盘；路径服务端生成固定 outputs/reports 前缀；Command 并入 state artifacts 复用既有 reducer。"
```

---

### Task 2: mysql-reporter Skill 报表流程与退役 MCP 残留清理

**Files:**
- Modify: `backend/package/yuxi/agents/skills/buildin/mysql-reporter/SKILL.md`
- Modify: `backend/test/unit/agents/test_builtin_discovery.py:191`、`backend/test/unit/services/skills/test_skill_service.py:624`（两处 `mcp_dependencies` 断言）

**Interfaces:**
- Consumes: Task 1 的工具名 `write_report_artifact`（skill 指令直接引用）。
- Produces: frontmatter `mcp_dependencies: ["saas-mcp"]`；SKILL.md 新增「产出报表 artifact」章节。test_builtin_discovery/test_skill_service 断言同步为 `["saas-mcp"]`。

- [ ] **Step 1: 先改两处测试断言为 `["saas-mcp"]`（RED）**
  Run: `uv run --group test pytest test/unit/agents/test_builtin_discovery.py test/unit/services/skills/test_skill_service.py -q`
  Expected: FAIL（断言实际仍为 `["saas-mcp", "mcp-server-chart"]`）
- [ ] **Step 2: 改 SKILL.md**：frontmatter `mcp_dependencies: ["saas-mcp"]`（`mcp-server-chart` 已在 `_RETIRED_BUILTIN_MCP_SERVER_SLUGS` 退役，残留引用会使门控解析悬挂）；正文新增章节「产出报表 artifact」：分析完成后调用 `write_report_artifact` 写入结构化报表——必填字段清单（与 Global Constraints schema 一致）、每条 metric 的 `source` 写明取数 SQL/脚本与口径、单报表规模建议（tables 行数 ≤ 200、charts 数据点 ≤ 500，S3 无分页的已知限制）、`validation_status` 仅在完成独立重算校验时才可写非 `agent_declared` 值之外的自评说明（首版一律 `agent_declared`）。
- [ ] **Step 3: 跑测确认 GREEN**
  Run: `uv run --group test pytest test/unit/agents/test_builtin_discovery.py test/unit/services/skills/test_skill_service.py test/unit/agents/skills/test_mysql_reporter_scripts.py -q`
  Expected: PASS
- [ ] **Step 4: Commit**
```bash
git add backend/package/yuxi/agents/skills/buildin/mysql-reporter/SKILL.md backend/test/unit/agents/test_builtin_discovery.py backend/test/unit/services/skills/test_skill_service.py
git commit -m "docs(skills): mysql-reporter 增加报表 artifact 产出流程" -m "同一变更移除 frontmatter 对已退役 mcp-server-chart 的残留依赖，避免门控解析悬挂；同步既有断言。"
```

---

### Task 3: 前端报表工具函数（纯 JS，单测）

**Files:**
- Create: `web/src/utils/reportWorkspace.js`
- Test: `web/test/unit/reportWorkspace.test.js`

**Interfaces:**
- Consumes: 无（纯函数，不 import 组件/API）。
- Produces（Task 4/5 依赖，签名钉死）:
  - `SUPPORTED_REPORT_SCHEMA_VERSION = 1`
  - `parseReportArtifact(text: string): { ok: boolean, report: object|null, error: string|null }`——JSON 解析失败/`schema_version` 非法或大于支持版本/缺必填字段（title、schema_version）时 `{ok:false, error:<中文>}`；成功透传完整对象（保留未知键）。
  - `buildReportRoute(threadId: string, artifactPath: string): string`——返回 `/report/<enc(threadId)>/<path 各段 encodeURIComponent>`。
  - `buildTablesCsv(table: {title?:string, columns:string[], rows:any[][]}): string`——RFC4180：含 `,` `"` 换行的单元格加引号并内嵌引号翻倍；行以 `\r\n` 连接；字符串前置 `﻿` BOM；数字/布尔转字符串，null/undefined 转空串。
  - `buildEchartsOption(chart: object): object`——bar/line：`{xKey, series:[{name,type,data: data.map(d=>d[dataKey])}], dataset}` 等价的普通 option（bar→`{series:{type:'bar'}}`，line 同理）；pie：`data.map(d=>({name:d[xKey], value:d[series[0].dataKey]}))`；type 非法抛 Error。
  - `buildCsvDownloadName(title: string): string`——`<sanitized title 或 'report'>-<YYYYmmdd-HHMMSS>.csv`，去除路径分隔符与控制字符。
- [ ] **Step 1: 写失败测试**（node --test + `ssrLoadModule`，参照 `web/test/unit/api_boundary.test.js` 的 harness 用法）：
  1. 最小合法报表 → `ok:true` 且 `report.title` 透传、未知键保留。
  2. `schema_version=2` → `ok:false`，error 含 `schema_version`。
  3. 非法 JSON 文本 → `ok:false`。
  4. 缺 title → `ok:false`。
  5. `buildReportRoute('t 1','u1/project/outputs/reports/a b.json')` → `/report/t%201/u1/project/outputs/reports/a%20b.json`。
  6. `buildTablesCsv({columns:['a,b','说"明'],rows:[['1','x\ny']]})` 断言 BOM 前缀、`"a,b"` 引号包裹、`"说""明"`、`"x\ny"`、`\r\n` 行尾。
  7. `buildEchartsOption` bar/pie 各一：断言 `series[0].type==='bar'` 与 pie `data[0].value` 取自 `dataKey`；`type:'scatter'` 抛 Error。
- [ ] **Step 2: RED**：`corepack pnpm run test:unit` 该文件 FAIL（模块不存在）
- [ ] **Step 3: 实现** `web/src/utils/reportWorkspace.js`（中文 docstring，DOM-free，可被 ssrLoadModule 加载——不得 import .vue 或浏览器全局）。
- [ ] **Step 4: GREEN**：`corepack pnpm run test:unit` 全量 PASS（426+新增）
- [ ] **Step 5: Commit**
```bash
git add web/src/utils/reportWorkspace.js web/test/unit/reportWorkspace.test.js
git commit -m "feat(web): 报表工作台纯函数工具集" -m "S3 验收：schema 解析/路由构建/CSV 转义/echarts option 均为可单测纯函数，供工具卡与独立页共用。"
```

---

### Task 4: 聊天工具卡 `write_report_artifact`

**Files:**
- Create: `web/src/components/ToolCallingResult/tools/ReportArtifactTool.vue`
- Modify: `web/src/components/ToolCallingResult/ToolCallRenderer.vue`（import + `TOOL_RENDERERS` 增 `write_report_artifact: ReportArtifactTool`）
- Modify: spec 验收矩阵第 2 行证据措辞（见 Step 5）

**Interfaces:**
- Consumes: props `toolCall/appearance/defaultExpanded`（与 `OcrParseFileTool.vue` 同构，参照其读取 args 与 result 的方式）；`buildReportRoute(threadId, artifactPath)`（Task 3）；`useRoute().params.thread_id` 取当前线程（工具卡不新增 threadId prop，与现有卡片一致从路由上下文取）。
- Produces: 卡片渲染：标题（args 里 report 的 title；args 为 JSON 字符串则解析，解析失败降级 `BaseToolCall` 行为——卡片内不吞错）；成功 ToolMessage 含路径时提取 `报表已写入 <path>` 的 path 生成「打开报表」`router-link`（`buildReportRoute(threadId, path)`）；失败（`Error:` 前缀）显示错误态不给链接。UI 用 ant-design-vue（卡片内不用 echarts）。

- [ ] **Step 1: 实现卡片**（先读 `web/src/components/ToolCallingResult/tools/OcrParseFileTool.vue` 与其测试方式——`.vue` 无 jsdom 单测基建，渲染正确性证据为 lint+build+手工；可测逻辑已在 Task 3）。
- [ ] **Step 2: 注册** `TOOL_RENDERERS`：`write_report_artifact: ReportArtifactTool`（字母序插入 map）。
- [ ] **Step 3: 验证**
  Run: `corepack pnpm run lint && corepack pnpm run build && corepack pnpm run test:unit`
  Expected: 全 PASS（单测确认 map 注册未破坏 `toolRegistry` 相关用例）
- [ ] **Step 4: spec 矩阵证据修正**：spec 验收矩阵第 2 行「直接证据/命令」改为 `unit：buildReportRoute（Task 3）；卡片渲染 lint+build+手工验证`，并在同文件验收矩阵该行「当前结果」补记（实现完成后由终审统一置 Passed 前的中间态保持 Not run）。
- [ ] **Step 5: Commit**
```bash
git add web/src/components/ToolCallingResult/tools/ReportArtifactTool.vue web/src/components/ToolCallingResult/ToolCallRenderer.vue docs/develop-guides/decisions/proposed/2026-10-10-finance-report-workspace.md
git commit -m "feat(web): 聊天流 write_report_artifact 报表卡片" -m "复用 TOOL_RENDERERS 工具卡机制；打开报表链接指向独立页；同步 spec 矩阵第 2 行证据措辞以匹配 .vue 无 jsdom 单测的实际能力。"
```

---

### Task 5: 独立报表页 `/report/:thread_id/:path(.*)*`

**Files:**
- Create: `web/src/views/ReportView.vue`
- Modify: `web/src/router/index.js`（AppLayout children 内、与 `/agent` 平级追加）

**Interfaces:**
- Consumes: Task 3 全部函数；`previewThreadArtifact(threadId, path)`（`web/src/apis/agent_api.js`，返回 blob Response）；`web/src/utils/dashboardCharts.js` 的 `init`（echarts 按需已注册 bar/line/pie + Grid/Legend/Tooltip）；ant-design-vue `a-table`/`a-drawer`/`a-alert`/`a-button`/`a-badge`。
- Produces: 路由记录 `{ path: 'report/:thread_id/:path(.*)*', name: 'ReportView', component: () => import('../views/ReportView.vue'), meta: { requiresAuth: true } }`（AppLayout children；完整 URL `/report/<threadId>/<path...>`）。页面行为：
  1. mount 时从 `route.params` 取 `thread_id` 与拼接 `path`（catch-all 数组用 `/` join，字符串直接用），`previewThreadArtifact` 拉取 → `.text()` → `parseReportArtifact`。
  2. `ok:false` → `a-alert type="error"` 显示 `error`（含超 `schema_version` 的明确中文提示），主体不渲染。
  3. `ok:true` → 标题/`generated_at`/`summary`；`a-badge` 展示 `validation_status === 'agent_declared' ? 'Agent 自评（未独立校验）' : validation_status`；metrics 用 `a-table`（`data-index` source 列给 `a-button type=link` 开 `a-drawer` 显示该行 `source`，无 source 则按钮隐藏）；tables 逐个 `a-table` + 各自「导出 CSV」按钮（`buildTablesCsv` → `new Blob([csv], {type:'text/csv;charset=utf-8'})` → `a` 元素 `download=buildCsvDownloadName(title)`，空 rows 不触发下载）；charts 逐个 `<div ref>`，onMounted/onUpdated 后 `init(el)` + `setOption(buildEchartsOption(chart))`，组件卸载 `dispose()`。
  4. fetch 非 2xx 或网络错误 → error alert（复用线程无权访问时 API 返回 404 的语义）。
- [ ] **Step 1: 实现页面与路由**（echarts 用法参照 `web/src/views/DashboardView.vue` 或任一 dashboard 组件的 `init` 生命周期模式）。
- [ ] **Step 2: 验证**
  Run: `corepack pnpm run lint && corepack pnpm run build && corepack pnpm run test:unit`
  Expected: 全 PASS
- [ ] **Step 3: 手工验证清单**（本机 docker 不可用，写入 PR 说明；有环境时执行）：合法报表渲染三区+抽屉+CSV；`schema_version=2` 文件显示提示不白屏；无权限线程 URL 得 404 提示；未登录跳转登录页。
- [ ] **Step 4: Commit**
```bash
git add web/src/views/ReportView.vue web/src/router/index.js
git commit -m "feat(web): 报表独立查看页" -m "S3 验收：复用既有 artifact 读取 API 与线程归属校验；schema 超版本 fail-visible；CSV 由纯函数导出。"
```

---

### Task 6: 全量验证与 spec 状态推进

**Files:**
- Modify: `docs/develop-guides/decisions/proposed/2026-10-10-finance-report-workspace.md` → 移动为 `docs/develop-guides/decisions/implemented/2026-10-10-finance-report-workspace.md`

**Interfaces:**
- Consumes: Task 1-5 全部产物。
- Produces: implemented 状态的决策记录（`## 问题/决策/替代方案/后果/验证` 标题结构；`## 提案` 标题改为 `## 决策` 并入实现要点；新增 `## 后果`、`## 验证` 段；验收矩阵「当前结果」列仅允许枚举 `Passed/Inspected/Not run/Inferred`——已验证行填 `Passed` 并附命令，未跑集成/手工行填 `Not run` 并写明原因）。

- [ ] **Step 1: 后端全量**
  Run: `uv run --group test pytest test/unit -q`（cwd=backend）
  Expected: PASS；同时 `uv run --group test pytest test/integration/api/test_skill_artifact_authorization.py -q` 若因缺服务栈失败则记录 Not run（不得写成通过）。
- [ ] **Step 2: 前端全量**：`corepack pnpm run test:unit && corepack pnpm run lint && corepack pnpm run build`
- [ ] **Step 3: 门禁与文档**（仓库根）：`python3 scripts/verify_engineering_contracts.py`、`cd docs && npm run build`；`git diff --check`。
- [ ] **Step 4: spec 推进 implemented**（标题改写 + 矩阵结果回填，参照 `implemented/2026-10-10-desktop-shell-v1.md` 的终态结构）。
- [ ] **Step 5: Commit**
```bash
git add docs/develop-guides/decisions/
git commit -m "docs(finance): S3 报表工作台决策记录推进 implemented" -m "回填验收矩阵真实结果；integration/手工项如实记 Not run 与原因。"
```

## 未验证范围（最终报告必列）

- `test/integration`、E2E 依赖 docker 服务栈，本机 Not run（host 仅 unit 全绿）。
- 卡片与独立页真实浏览器渲染、echarts 视觉、CSV Excel 打开：手工验证 Not run（无运行中后端）。
- linko_ent 粒度与财务示例指标口径匹配性：spec 风险段已记，S3 不验证。
