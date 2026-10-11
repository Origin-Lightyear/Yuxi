# 财务工作台 S3：报表 Artifact 与独立查看页

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/toolkits/buildin/tools.py

## 问题

财务场景（mysql-reporter + saas-mcp 的 linko_ent）的分析结果只能以聊天文本、SQL 结果表（`MysqlQueryTool.vue` 自定义 markup）或 markdown/html:preview 交付：没有可复用的结构化报表载体（指标 + 表格 + 图表 + 导出 + 逐指标溯源），也没有独立查看页可分享与打印。聊天流内渲染复杂报表会拖垮消息气泡；每次分析重跑 SQL 无法沉淀为可回看的产物。S1/S2 已完成数据通路（mysql-reporter 脚本 + linko_ent），S3 补齐交付层。

## 决策

跨 Owner 分工：`backend/package/yuxi/agents/toolkits/` 拥有工具注册与报表 JSON 写入校验；`backend/package/yuxi/agents/skills/buildin/mysql-reporter/SKILL.md` 拥有「何时写报表」的流程指令；`web/src/components/ToolCallingResult/` 拥有聊天内报表卡片；`web/src/views/ReportView.vue` 与 `web/src/router/index.js` 拥有独立查看页；artifact 读取复用既有 `GET /api/chat/thread/{thread_id}/artifacts/{path}`（`chat_router.py` + `artifact_service.resolve_thread_artifact_view`），其线程归属校验即本页授权边界。

### 实现方案

报表以**文件制 artifact** 交付，不新增 DB 表、不新增读取 API：

- **报表格式**：`{workdir}/outputs/reports/report-<UTC时间戳>.json`，内容为固定 schema 的 JSON 对象——`schema_version`（整数，首版 1）、`title`、`generated_at`、`summary`、`metrics[]`（`key/label/value/unit`，可选 `source` 溯源描述对象）、`tables[]`（`title/columns/rows`）、`charts[]`（限 `bar|line|pie` 三型，`xKey/series/data`，复用 `web/src/utils/dashboardCharts.js` 已按需注册的类型）、`validation_status`（首版仅 `agent_declared`，由生产 Agent 自评并明示）。
- **写入工具** `write_report_artifact`（`toolkits/buildin` 新模块，`@tool` 注册）：入参为报表对象，同步做结构校验（必填字段、类型、charts 限三型、`schema_version ≤ 当前支持版本`），校验失败直接报错不落盘（fail-closed）；通过后写入 `outputs/reports/`（重名消解沿用 `ocr_parse_file` 的 `-1` 追加约定），并以 `Command(update={"artifacts": ...})` 把路径并入 state（沿用 `present_artifacts` 的合并方式）。默认 `tools="all"` 集合包含该工具，不依赖 Skill 门控即可用。
- **财务流程**：`mysql-reporter/SKILL.md` 增加「分析完成后将核心指标、明细表、图表与逐指标 `source` 写入 `write_report_artifact`」指令；同一变更中移除 frontmatter 对已退役 `mcp-server-chart` 的 `mcp_dependencies` 残留（`mcp/service.py:_RETIRED_BUILTIN_MCP_SERVER_SLUGS` 已删除该服务器）。
- **聊天卡片**：`ToolCallRenderer.vue` 的 `TOOL_RENDERERS` 注册 `write_report_artifact` 专用卡（`BaseToolCall` 基座）：显示标题、生成时间、指标数摘要与「打开报表」链接（跳 `/report/...`）。
- **独立页** `/report/:thread_id/:path(.*)*`（AppLayout 子路由，`meta.requiresAuth`）：`ReportView.vue` 经既有 `getThreadArtifactUrl/previewThreadArtifact`（`agent_api.js`）取 JSON——授权由既有 artifact API 的线程归属校验承担；页内以 `a-table` 渲染 `metrics` 与 `tables`，`dashboardCharts.js` 的 echarts 实例渲染 `charts`，每行指标可点开溯源抽屉显示 `source`，并提供 CSV 导出（前端 Blob 下载，逐表导出）；`validation_status` 以 badge 展示；`schema_version` 高于前端支持版本时显示明确提示并不渲染主体。
- **通知卡片联动**：终态通知与定时任务通知不改（S3 零改动 `desktopNotify`/`scheduledRunPoller`）。

## 替代方案

- 为 artifact 建 DB 表 + 版本管理 + 复核工作流：满足版本比较与复核诉求，但引入 schema 迁移与 Owner 面，S4+ 再议；S3 的文件制与其不冲突（JSON 可平移）。
- 复用 `MysqlQueryTool.vue` 的结果表渲染承载报表：无图表、无导出、无溯源、无独立页，且把分析产物锁死在单次工具调用折叠区里，拒绝。
- 以 markdown 或 `html:preview` 围栏交付报表：无结构化导出与逐指标溯源，消费端不可编程解析，拒绝。

## 后果

- 定时任务不产出报表：报表只在会话内由 Agent 调用 `write_report_artifact` 产生；定时任务直出报表依赖 S4 的调度与 artifact 绑定，为明确非目标。
- `validation_status` 为 Agent 声明：首版无独立 oracle 复核数值正确性，卡片与页面 badge 明示「Agent 自评」，不伪装为已校验。
- 超大报表无分页：`charts[].data` 与 `tables[].rows` 无分页约束，超大数据集时页面渲染与 CSV 导出可能卡顿；S3 以 skill 指令限制示例规模，不引入虚拟滚动。
- linko_ent 数据粒度与财务示例指标口径匹配性未在 S3 验证：不匹配时财务示例指标回退 SQL 直查口径，由 skill 示例记录。
- integration（跨用户越权 404）与真实浏览器手工验证（卡片渲染、独立页渲染、echarts 视觉、CSV Excel 打开）未在本机执行：无 docker 服务栈与运行中后端。
- `schema_version` 演进无向后兼容转换层：前后端各自持有支持版本常量，升级顺序错位由「超版本即提示」路径兜底。

## 验证

本机（host uv，无 docker 服务栈）实际执行的命令与结果：

- `cd backend && uv run --group test pytest test/unit -q`：2641 passed，6 warnings（含既有 `test_agent_stream_close.py` 的 LangChainBetaWarning 噪声），87.22s。
- `uv run --group test pytest test/unit/agents/toolkits/buildin/test_write_report_artifact.py -q`：27 passed。
- `uv run --group test pytest test/unit/agents/test_builtin_discovery.py test/unit/services/skills -q`：135 passed。
- `cd web && corepack pnpm run test:unit`：427 pass / 0 fail；`corepack pnpm run lint:check` 通过；`corepack pnpm run build` 通过。
- `python3 scripts/verify_engineering_contracts.py`：通过（49 decisions / 5 workflows / 4 agents files / 189 docs / 27 routers / 255 web sources）；`python3 -m unittest scripts.test_verify_engineering_contracts`：63 tests OK。
- `cd docs && npm run build`：build complete in 3.84s（先 `npm install` 安装本机缺失依赖）。
- `git diff e2a5f5bf..HEAD --name-only`：改动集 14 代码文件（+1 spec），`desktopNotify.js`/`scheduledRunPoller.js` 命中数 0。
- `uv run --group test pytest test/integration/api/test_skill_artifact_authorization.py -q`：setup 即 `RuntimeError: SANDBOX_PROVISIONER_TOKEN is required for integration sandbox cleanup`——本机无 docker 服务栈，记 Not run，不写成通过。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 工具校验通过后 JSON 落盘 `outputs/reports/` 且 state artifacts 含该路径 | 静默写坏文件或路径不进交付物列表 | `toolkits` 报表工具 | `uv run --group test pytest test/unit/agents/toolkits/buildin/test_write_report_artifact.py -q`：27 passed | 缺必填字段/类型错误/超支持 `schema_version` 时断言报错且无文件产生 | Passed |
| 聊天流出现报表卡片，标题正确且「打开报表」链接指向独立页 | 卡片缺失或链接 404 | `ToolCallRenderer` 报表卡 | lint+build 通过；`buildReportRoute` 纯函数单测 Passed；真实页面手工验证 Not run | 无卡片工具回退 `BaseToolCall` 不受影响 | Inspected |
| 独立页渲染 metrics 表、tables、三型 charts 与溯源抽屉 | 只显示原始 JSON 或图表空白 | `ReportView.vue` | lint+build+`parseReportArtifact` 纯函数单测 Passed；页面渲染手工验证 Not run | `schema_version` 超支持版本时显示提示不渲染主体 | Inspected |
| CSV 导出内容与 tables 一致 | 导出错列/丢行/编码损坏 | `ReportView.vue` 导出 | `buildTablesCsv` 纯函数单测 Passed（逗号/引号/中文/BOM/CRLF）；浏览器下载与 Excel 打开手工验证 Not run | 空 tables 不产生下载 | Inspected |
| 无权限用户访问报表 artifact 返回 404/401 | 越权读他人报表 | 既有 `artifact_service.resolve_thread_artifact_view` | `uv run --group test pytest test/integration/api/test_skill_artifact_authorization.py -q`：本机 setup 即缺 `SANDBOX_PROVISIONER_TOKEN`（无 docker 服务栈）；既有 API 未改动 | 跨用户 404；未登录 401 | Not run |
| mysql-reporter skill 改动后内置发现与门控测试仍通过 | skill 失联或误门控 | `services/skills/shared.py` 既有测试 | `uv run --group test pytest test/unit/agents/test_builtin_discovery.py test/unit/services/skills -q`：135 passed | frontmatter 去除已退役 MCP 后门控解析不报错 | Passed |
| 定时任务/终态通知行为零变化 | S3 误改通知链路 | `desktopNotify.js`/`scheduledRunPoller.js` 不在改动集 | `git diff e2a5f5bf..HEAD --name-only`：两文件命中数 0；web unit 427 全绿 | 改动集不含该两文件 | Passed |
| 后端 unit 全量、web lint+unit+build、contracts、docs build 全部通过 | 回归破坏 | 各命令 Owner | 后端 unit 2641 passed；web 427 pass + lint + build；contracts + unittest gate 通过；docs build complete 3.84s；integration 未执行，见上表越权行 Not run | 正向命令 | Passed |
