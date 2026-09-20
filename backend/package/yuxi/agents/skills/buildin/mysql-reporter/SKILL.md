---
name: mysql reporter
slug: mysql-reporter
description: "生成 MySQL 查询报表并生成可视化图表。当用户需要查询 MySQL 数据库并以报表形式展示结果时使用此技能，包括：统计销售数据、分析用户行为、生成业务报表、查询业务指标等。"
---

# MySQL 报表技能

根据用户的数据来源选择查询方式，并结合图表绘制工具构建查询报告。

## 数据源选择

### SaaS 企业数据

用户查询当前企业内的公司、门店、员工、用户、订单等业务数据时：

1. 优先调用 SaaS MCP 的 `select_all` 工具
2. `message` 参数直接传入清晰完整的自然语言查询需求，不要自行拼接 SQL
3. 使用工具返回的数据整理结果；需要可视化时再调用 Charts MCP

如果 `select_all` 不可用，或返回 `Unauthorized`、`AgentSession` 不存在或已过期，应明确提示用户退出后重新登录。不要回退到沙盒 MySQL，也不要要求用户配置 `MYSQL_*` 环境变量。

### 独立外部 MySQL

仅当用户明确要查询当前企业数据源之外的 MySQL，或明确提供了沙盒 MySQL 配置时，才使用以下流程。

## 操作流程

1. 理解用户的指令，明确报表的需求和目标
2. 通过 terminal 进入技能目录：`cd /home/gem/skills/mysql-reporter`
3. 使用 `uv run scripts/list_tables.py` 查看可用表；如果脚本提示缺少 MySQL 配置，按“环境变量缺失处理”回复用户
4. 必要时用 `uv run scripts/describe_table.py --table 表名` 查看表结构
5. 生成正确且高效的只读 SQL，通过 `uv run scripts/query.py --sql "SQL语句" --timeout 60` 执行查询并获取结果
6. 使用 Charts MCP 生成图表
7. 将图表以 markdown 图片格式嵌入报表

## 环境变量缺失处理

脚本只读取 Agent 沙盒中的环境变量，不读取后端 `.env` 或 Docker Compose 变量。必填变量包括：

- `MYSQL_HOST`
- `MYSQL_USER`
- `MYSQL_PASSWORD`
- `MYSQL_DATABASE`

可选变量包括：

- `MYSQL_PORT`：默认 `3306`
- `MYSQL_DATABASE_DESCRIPTION`：数据库业务说明，用于辅助理解表和指标含义

如果执行脚本时出现 `MySQL configuration missing required key`，不要继续猜测连接信息或编造报表。应明确告诉用户：需要在个人设置中的「沙盒环境变量」里配置缺失的 `MYSQL_*` 变量；保存后仅对新建沙盒生效，需要重新发起任务或新建会话后再执行。

## 关键约束

- 独立外部 MySQL 的 SQL 查询必须正确且高效，避免全表扫描
- SaaS 企业数据必须通过 `select_all` 查询；独立外部 MySQL 必须通过本技能 `scripts/` 下的 CLI 脚本查询
- 不要在报表或错误说明中输出 `MYSQL_PASSWORD` 等敏感环境变量的值，只能说明缺少哪些变量名
- 图表生成工具的返回结果不会默认渲染，必须在最终报表中以 `![描述](图片URL)` 格式嵌入
- 只返回报表相关的结论，不要返回原始 SQL 查询语句

## 允许的工具

- SaaS MCP `select_all`：查询当前员工有权访问的企业数据
- terminal：执行 `scripts/list_tables.py`、`scripts/describe_table.py`、`scripts/query.py`
- Charts MCP：生成可视化图表
- 网络检索工具：必要时补充背景信息
