# 知识库权限管理

本文档说明知识库的权限模型、权限设置方式，以及全部知识库相关接口的入参、返参与权限要求。

## 权限模型

知识库采用三级权限模型：

| 权限等级 | 说明 |
|----------|------|
| `NONE` | 无权限，无法访问知识库 |
| `READ` | 只读权限，可查询/检索知识库内容 |
| `MANAGE` | 管理权限，可编辑/删除/配置知识库 |

## 权限解析流程

权限解析核心逻辑位于 `backend/package/yuxi/permissions/resource_permission.py`。

```
用户请求 → 权限解析 → 返回有效权限
```

### 解析优先级

1. **超级管理员（superadmin）** → 直接返回 `MANAGE`
2. **资源创建者** → 返回 `MANAGE`
3. **匹配共享范围（share_config）**：
   - 匹配 `manage_scope` 且匹配 `read_scope` → `MANAGE`
   - 仅匹配 `read_scope` → `READ`
   - 都不匹配 → `NONE`
4. **角色上限约束**：取 granted 权限和角色上限中的较小值

### 角色权限上限

| 角色 | 最高可获得权限 |
|------|----------------|
| `superadmin` | MANAGE（所有知识库） |
| `admin` | MANAGE |
| `user` | MANAGE（仅表达「文档级管理」，见下） |

> **员工 MANAGE 边界**（SaaS 场景）：普通员工命中 `manage_scope` 后获得 MANAGE，但仅限
> **文档内容操作**（上传/删除文档）；知识库 CRUD、目录增删改、share_config 修改、
> 图谱构建等结构操作仍由端点的管理员门槛拦截（员工一律 403）。员工的敏感参数
> （密钥等）在响应中始终脱敏。

## 共享配置（share_config）

### 结构定义

```json
{
  "version": 2,
  "read_scope": {
    "access_level": "global|department|user",
    "department_ids": [],
    "user_uids": []
  },
  "manage_scope": {
    "access_level": "global|department|user",
    "department_ids": [],
    "user_uids": []
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | int | 配置版本，固定为 2 |
| `read_scope` | object | 读取权限范围 |
| `manage_scope` | object | 管理权限范围 |
| `access_level` | string | 范围类型：global/department/user |
| `department_ids` | array | 部门 ID 列表（access_level=department 时使用） |
| `user_uids` | array | 用户 UID 列表（access_level=user 时使用） |

### 范围类型

| access_level | 说明 | 必填字段 |
|--------------|------|----------|
| `global` | 全局可见，所有用户可访问 | 无 |
| `department` | 指定部门可见 | department_ids |
| `user` | 指定用户可见 | user_uids |

### 约束规则

- `manage_scope` 必须包含在 `read_scope` 范围内（服务端校验，越界配置保存时拒绝）
- 部门范围：manage_scope 的 department_ids 必须是 read_scope 的子集
- 用户范围：manage_scope 的 user_uids 必须是 read_scope 的子集
- `read_scope=global` 时 manage_scope 可任意收敛（如「全员可读 + 指定员工可管理」）
- 单个 scope 内 global / department / user 三选一，不能同时混填多个维度
- **空 `user_uids` 合法**：表达「无员工权限」（租户平台按角色展开为空时使用），任何用户都不命中
- SaaS 租户场景：`user_uids` 使用员工的外部标识 `employeeCode`（SaaS 员工登录 Yuxi 时本地 `uid = employeeCode`，同一套标识）

### 在哪里设置

`share_config` 通过两个接口写入：**创建知识库**（`POST /databases`）和**更新知识库**（`PUT /databases/{kb_id}`）。没有独立的"权限设置"接口——更新接口的 `share_config` 字段就是权限设置入口，详见下文接口说明。

## API 总览

### 通用约定

- 所有接口前缀为 `/api/knowledge`（下文路径省略此前缀），需要 `Authorization: Bearer <token>`。
- 权限校验分两级：**角色门槛**（依赖 `get_admin_user` / `get_required_user`，见下）+ **资源权限**（依赖 `require_knowledge_base_read` / `require_knowledge_base_manage`，按上文解析流程计算）。

### 权限校验依赖

| 依赖函数 | 角色门槛 | 资源权限 | 用途 |
|----------|----------|----------|------|
| `get_required_user` | 任意登录用户（需绑定部门） | — | 可访问列表等通用接口 |
| `get_admin_user` | admin / superadmin | — | 知识库列表等管理级接口 |
| `require_knowledge_base_read` | admin / superadmin | READ | 查询、检索、导出、文档查看 |
| `require_knowledge_base_manage` | admin / superadmin | MANAGE | 创建、更新、删除、配置、文档变更 |

> 注意：知识库级接口（带 `{kb_id}` 的路径）均以 `get_admin_user` 为角色门槛，在此基础上再解析资源权限。普通用户（role=user）通过 `GET /databases/accessible` 获取可访问列表。

### 知识库对象公共字段

列表与详情接口返回的知识库对象（`serialize_knowledge_base`）包含以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `kb_id` | string | 知识库 ID |
| `name` | string | 名称 |
| `description` | string | 描述 |
| `kb_type` | string | 类型（默认 `milvus`） |
| `embedding_model_spec` | string \| null | 嵌入模型 spec |
| `llm_model_spec` | string \| null | LLM 模型 spec |
| `query_params` | object | 查询参数配置 |
| `metadata` | object | 扩展元数据 |
| `created_by` | string | 创建者 UID |
| `created_at` | string | 创建时间（ISO） |
| `status` | string | 连接状态（`已连接`） |
| `stats` | object | 统计信息（文件数/Chunk 数/Token 数等） |
| `row_count` | int | 行数（列表接口回退为文件数） |
| `share_config` | object \| null | 共享配置（见上节） |
| `additional_params` | object | 附加参数（含图谱配置等） |
| `effective_permission` | string | 当前用户的有效权限：`NONE`/`READ`/`MANAGE` |
| `can_manage` | bool | 是否有管理权限 |
| `mindmap` | object | 思维导图数据（仅详情接口） |
| `sample_questions` | array | 示例问题（仅详情接口） |
| `files` | object | 文件列表（仅详情接口 `include_files=true` 时） |

## 知识库管理接口

### 创建知识库

**接口**：`POST /knowledge/databases`

**权限要求**：admin / superadmin

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `database_name` | string | 是 | 知识库名称 |
| `description` | string | 是 | 知识库描述 |
| `embedding_model_spec` | string | 否 | 嵌入模型 spec |
| `llm_model_spec` | string | 否 | LLM 模型 spec |
| `kb_type` | string | 否 | 类型，默认 `milvus` |
| `additional_params` | object | 否 | 附加参数 |
| `share_config` | object | 否 | 共享配置（创建时即设置权限） |

**响应**：知识库对象（见公共字段）+ `files: {}`；名称冲突返回 409。

```json
{
  "database_name": "产品知识库",
  "description": "产品手册与 FAQ",
  "kb_type": "milvus",
  "share_config": {
    "version": 2,
    "read_scope": {"access_level": "department", "department_ids": [5], "user_uids": []},
    "manage_scope": {"access_level": "user", "user_uids": []}
  }
}
```

### 获取知识库列表

**接口**：`GET /knowledge/databases`

**权限要求**：admin / superadmin（按用户权限过滤，仅返回有权限的知识库）

**响应**：

```json
{
  "databases": [ /* 知识库对象数组，含 effective_permission */ ]
}
```

### 获取可访问知识库列表

**接口**：`GET /knowledge/databases/accessible`

**权限要求**：任意登录用户（需绑定部门）

**说明**：返回当前用户有权限访问的知识库精简列表，用于智能体配置等场景。

**响应**：

```json
{
  "databases": [
    {
      "name": "产品知识库",
      "kb_id": "kb_xxx",
      "description": "产品手册与 FAQ",
      "created_by": "uid_001",
      "kb_type": "milvus",
      "supports_documents": true
    }
  ]
}
```

### 获取知识库详情

**接口**：`GET /knowledge/databases/{kb_id}`

**权限要求**：READ

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `include_files` | bool | 否 | 是否包含全量文件列表，默认 `false`（大知识库避免响应过大） |

**响应**：知识库对象（含 `mindmap`、`sample_questions`，`include_files=true` 时含 `files`/`files_truncated`/`files_page_size`）。非 MANAGE 权限时敏感参数（密钥等）会被脱敏。

### 更新知识库（含设置权限）

**接口**：`PUT /knowledge/databases/{kb_id}`

**权限要求**：MANAGE

**请求体**（`UpdateDatabaseRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 名称 |
| `description` | string | 是 | 描述 |
| `llm_model_spec` | string \| null | 否 | LLM 模型 spec（仅当字段出现在请求体时更新） |
| `additional_params` | object | 否 | 附加参数 |
| `share_config` | object | 否 | **权限设置入口**，结构见「共享配置」一节 |

**响应**：`{"message": "更新成功", "database": <知识库对象>}`

**设置权限示例**——将知识库设为部门可读、指定用户可管理：

```json
{
  "name": "产品知识库",
  "description": "产品手册与 FAQ",
  "share_config": {
    "version": 2,
    "read_scope": {"access_level": "department", "department_ids": [1, 2], "user_uids": []},
    "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": ["uid_001"]}
  }
}
```

### 删除知识库

**接口**：`DELETE /knowledge/databases/{kb_id}`

**权限要求**：MANAGE

**响应**：`{"message": "删除成功"}`

### 导出知识库

**接口**：`GET /knowledge/databases/{kb_id}/export`

**权限要求**：READ

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `format` | string | 否 | 导出格式：`csv` / `xlsx` / `md` / `txt`，默认 `csv` |
| `include_vectors` | bool | 否 | 是否包含向量数据，默认 `false` |

**响应**：文件流（下载文件）。

## 目录（文件夹）接口

> 概念区分：「目录」是**知识库内部的文件夹树**，与「创建知识库」是两个层级——
> `POST /databases` 创建知识库本体；`POST /databases/{kb_id}/folders` 在已有知识库内创建文件夹。
> 文件夹层级通过 `parent_id` 表达，文件与文件夹统一由文档接口管理（移动、列举均按父级 ID）。

### 创建文件夹

**接口**：`POST /knowledge/databases/{kb_id}/folders`

**权限要求**：MANAGE

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `folder_name` | string | 是 | 文件夹名称 |
| `parent_id` | string \| null | 否 | 父文件夹 ID，空表示根目录 |

**响应**：新文件夹对象（含 `folder_id` 等）。

### 移动文件或文件夹

**接口**：`PUT /knowledge/databases/{kb_id}/documents/{doc_id}/move`

**权限要求**：MANAGE

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `new_parent_id` | string \| null | 是 | 目标父文件夹 ID，空表示移动到根目录 |

**说明**：`{doc_id}` 既可以是文件也可以是文件夹 ID。**响应**：移动结果。

### 按目录列举

目录树的遍历通过文档列表接口的 `parent_id` 参数实现，见下文 `GET /databases/{kb_id}/documents`。

## 文档管理接口

### 获取文档列表（按目录/分页）

**接口**：`GET /knowledge/databases/{kb_id}/documents`

**权限要求**：READ

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `parent_id` | string \| null | 否 | 父文件夹 ID，空表示根目录 |
| `path_prefix` | string \| null | 否 | 路径型目录前缀（懒加载 source_path 虚拟目录） |
| `status` | string | 否 | 文件状态筛选，默认 `all` |
| `page` | int | 否 | 页码，从 1 开始 |
| `page_size` | int | 否 | 每页数量，默认 100，最大 500 |
| `recursive` | bool | 否 | 是否跨目录筛选，默认 `false` |

**响应**：分页文件列表（含文件元数据与文件夹条目）。

### 按文件名搜索

**接口**：`GET /knowledge/databases/{kb_id}/documents/search`

**权限要求**：READ

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 否 | 文件名关键词（仅匹配文件名，不匹配内容） |
| `offset` | int | 否 | 偏移量，默认 0 |
| `limit` | int | 否 | 每页数量，默认 100，最大 500 |

**响应**：`{"files": [...], "total": 0, "offset": 0, "limit": 100, "has_more": false}`（空关键词直接返回空结果）。

### 检查文件是否存在

**接口**：`GET /knowledge/databases/{kb_id}/documents/exists`

**权限要求**：READ

**查询参数**：`filename`（string，必填）——文件展示名或相对路径。

**响应**：`{"kb_id": "...", "filename": "...", "exists": true|false}`

### 添加文档

**接口**：`POST /knowledge/databases/{kb_id}/documents`

**权限要求**：MANAGE

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `items` | string[] | 是 | 文件路径列表 |
| `params` | object | 是 | 处理参数，见下 |

`params` 常用字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `content_type` | string | 固定 `file`（URL 请先走 `POST /files/fetch-url`） |
| `auto_index` | bool | 解析后是否自动入库，默认 `false` |
| `chunk_preset_id` | string | 分块预设 ID（可选，见 `GET /chunk-presets`） |
| `chunk_parser_config` | object | 分块解析配置（可选） |

**说明**：上传 → 解析 → 可选入库为异步任务。**响应**：任务信息（含 `task_id`）。

### 入库已上传文档

**接口**：`POST /knowledge/databases/{kb_id}/documents/add`

**权限要求**：MANAGE

**请求体**：`{"items": ["path1"], "params": {...}}`（`params` 可选，结构同上）。

### 批量解析 / 入库

| 接口 | 请求体 | 说明 |
|------|--------|------|
| `POST /knowledge/databases/{kb_id}/documents/parse` | `["file_id1", "file_id2"]`（file_ids 数组） | 批量解析指定文档 |
| `POST /knowledge/databases/{kb_id}/documents/parse-pending` | — | 解析全部待处理文档 |
| `POST /knowledge/databases/{kb_id}/documents/index` | `["file_id1"], params` 或仅数组 | 批量入库已解析文档（`params` 可选，可覆盖处理参数） |
| `POST /knowledge/databases/{kb_id}/documents/index-pending` | `{"params": {...}}`（可选） | 入库全部已解析文档 |

**权限要求**：MANAGE（SaaS 员工按目标文档目录 edit 判定，见「员工端点权限一览」）。

### 文档详情 / 内容 / 下载

| 接口 | 权限 | 说明 |
|------|------|------|
| `GET /knowledge/databases/{kb_id}/documents/{doc_id}` | READ | 文档完整详情（解析状态、Chunk 统计等） |
| `GET /knowledge/databases/{kb_id}/documents/{doc_id}/basic` | READ | 文档基础信息（meta 等） |
| `GET /knowledge/databases/{kb_id}/documents/{doc_id}/content` | READ | 文档解析内容 |
| `GET /knowledge/databases/{kb_id}/documents/{doc_id}/download` | READ | 下载原始文件（文件流） |

### 删除文档 / 文件夹

| 接口 | 权限 | 请求体 | 说明 |
|------|------|--------|------|
| `DELETE /knowledge/databases/{kb_id}/documents/{doc_id}` | MANAGE | — | 删除单个文档 |
| `DELETE /knowledge/databases/{kb_id}/documents/batch` | MANAGE | `["id1", "id2"]`（file_ids 数组） | 批量删除文档或文件夹 |

**批量删除响应**：`{"message": ..., "deleted_count": n, "failed_items": [...]}` 等执行结果。

## 图谱构建接口

### 概念

图谱构建是指用 LLM 从知识库的 Chunk 中**抽取实体与关系**，构建知识图谱并写入向量库的过程。整体流程：

```
1. config  锁定抽取配置（抽取器类型 + 模型 + Schema 约束）
2. index   提交构建任务（后台异步执行）
3. status  查询构建进度与统计
4. failed-chunks  排查抽取失败的 Chunk
5. reset / reconcile  重置状态或修复向量索引
```

### 配置图谱抽取

**接口**：`POST /knowledge/databases/{kb_id}/graph-build/config`

**权限要求**：MANAGE

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `extractor_type` | string | 是 | 抽取器类型，目前仅支持 `llm` |
| `extractor_options` | object | 否 | 抽取器参数，见下 |

`extractor_options`（llm 抽取器）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model_spec` | string | 是 | 抽取用的 LLM 模型 spec |
| `schema` | string \| object | 否 | 抽取 Schema 约束（限定实体/关系类型），不支持自定义完整 prompt |
| `concurrency_count` | int | 否 | LLM 并发数，1–1000，默认 1 |
| `model_params` | object | 否 | 模型调用参数（如 temperature） |

**锁定规则**：首次配置后 `locked=true`；已锁定时不能更换 `extractor_type`（返回 409），只能修改模型、Schema 等抽取参数。

**响应**：`{"message": "图谱抽取配置已锁定", "status": "success", "config": {...}}`

**示例**：

```json
{
  "extractor_type": "llm",
  "extractor_options": {
    "model_spec": "gpt-4o",
    "schema": {"entity_types": ["人物", "机构"], "relation_types": ["任职于", "合作"]},
    "concurrency_count": 4
  }
}
```

### 执行图谱构建

**接口**：`POST /knowledge/databases/{kb_id}/graph-build/index`

**权限要求**：MANAGE

**说明**：对全部待处理 Chunk 执行图谱抽取与入库（后台任务）。要求已锁定抽取配置，否则返回 400；已有运行中的构建任务返回 409。

**响应**：`{"message": "图谱构建任务已提交", "status": "queued", "task_id": "..."}`

### 查询构建状态

**接口**：`GET /knowledge/databases/{kb_id}/graph-build/status`

**权限要求**：READ

**响应**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `kb_id` / `kb_type` | string | 知识库标识 |
| `configured` | bool | 是否已配置 |
| `locked` | bool | 配置是否已锁定 |
| `config` | object | 抽取配置（公开字段） |
| `total_chunks` / `pending_chunks` / `indexed_chunks` / `structured_chunks` | int | Chunk 统计 |
| `extraction_counts` / `vector_counts` | object | 抽取/向量状态统计 |
| `entity_count` / `relationship_count` | int | 已抽取的实体/关系数量 |
| `build_task_status` | string | 任务状态：`pending`/`running`/`completed`/`failed`/null |
| `build_task_progress` | int | 任务进度百分比 |

### 查看抽取失败 Chunk

**接口**：`GET /knowledge/databases/{kb_id}/graph-build/failed-chunks`

**权限要求**：READ

**查询参数**：`limit`（int，1–10，默认 10）——失败样例数量。

**响应**：失败 Chunk 样例列表。

### 重置图谱构建状态

**接口**：`POST /knowledge/databases/{kb_id}/graph-build/reset`

**权限要求**：MANAGE

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `clear_extraction_result` | bool | 否 | 清除抽取结果，默认 `true` |
| `clear_config` | bool | 否 | 同时清除抽取配置（解除锁定），默认 `false` |

**说明**：存在运行中的构建任务时返回 409。

### 修复图谱向量索引

**接口**：`POST /knowledge/databases/{kb_id}/graph-build/reconcile`

**权限要求**：MANAGE

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mode` | string | 否 | `failed`（仅修复失败向量，默认）或 `all_vectors`（全量重建） |

**说明**：先修复向量索引，再补跑待处理 Chunk（后台任务）；运行中任务存在时返回 409。

**响应**：`{"message": "图谱向量索引修复任务已提交", "status": "queued", "task_id": "...", "mode": "failed"}`

## 查询接口

### 查询知识库

**接口**：`POST /knowledge/databases/{kb_id}/query`

**权限要求**：READ

**请求体**：`{"query": "问题文本", ...meta}`——`query` 为问题，其余字段作为查询参数（如 `top_k`）透传给检索。

**响应**：`{"result": <检索结果>, "status": "success"}`（失败时 `status: "failed"` + `message`）。

### 测试查询

**接口**：`POST /knowledge/databases/{kb_id}/query-test`

**权限要求**：READ

**请求体**：同上（`query` + `meta`）。**响应**：直接返回检索原始结果（用于调试页面）。

### 查询参数配置

| 接口 | 权限 | 请求体 / 响应 |
|------|------|---------------|
| `PUT /knowledge/databases/{kb_id}/query-params` | MANAGE | 请求体：`{...查询参数}`；响应 `{"message": "success", "data": {...}}` |
| `GET /knowledge/databases/{kb_id}/query-params` | READ | 响应 `{"params": {...}, "message": "success"}` |

## 辅助接口

| 接口 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/knowledge/mindmap/databases` | GET | admin | 知识库概览（思维导图选择界面） |
| `/knowledge/databases/{kb_id}/mindmap/files` | GET | READ | 思维导图文件列表 |
| `/knowledge/databases/{kb_id}/mindmap/generate` | POST | MANAGE | 生成思维导图 |
| `/knowledge/databases/{kb_id}/mindmap` | GET | READ | 获取思维导图数据 |
| `/knowledge/databases/{kb_id}/mindmap/diff` | GET | READ | 思维导图差异对比 |
| `/knowledge/databases/{kb_id}/stats/repair` | POST | MANAGE | 修复历史文件缺失的 Chunk/Token 统计 |
| `/knowledge/databases/{kb_id}/sample-questions` | POST | MANAGE | AI 生成测试问题（请求体 `{"count": 10}`） |
| `/knowledge/databases/{kb_id}/sample-questions` | GET | READ | 获取已生成的测试问题 |
| `/knowledge/files/fetch-url` | POST | admin | 抓取 URL 内容入库（请求体含 `kb_id`、`url`） |
| `/knowledge/files/import-workspace` | POST | admin | 从工作区导入文件（`{"kb_id": "...", "paths": [...]}`） |
| `/knowledge/files/upload` | POST | admin | 上传文件（multipart） |
| `/knowledge/files/supported-types` | GET | admin | 支持的文件类型 |
| `/knowledge/files/markdown` | POST | admin | 从 Markdown 文本创建文档 |
| `/knowledge/types` | GET | admin | 知识库类型列表 |
| `/knowledge/chunk-presets` | GET | admin | 分块预设选项 |
| `/knowledge/stats` | GET | admin | 知识库全局统计 |
| `/knowledge/generate-description` | POST | admin | AI 生成知识库描述 |

## 接口权限一览

| 接口 | 方法 | 角色门槛 | 资源权限 | 说明 |
|------|------|----------|----------|------|
| `/knowledge/databases` | GET | admin | — | 知识库列表（按权限过滤） |
| `/knowledge/databases` | POST | admin | — | 创建知识库（可带 share_config） |
| `/knowledge/databases/accessible` | GET | 登录用户 | — | 可访问知识库精简列表 |
| `/knowledge/databases/{kb_id}` | GET | admin | READ | 知识库详情 |
| `/knowledge/databases/{kb_id}` | PUT | admin | MANAGE | 更新信息 / **设置权限** |
| `/knowledge/databases/{kb_id}` | DELETE | admin | MANAGE | 删除知识库 |
| `/knowledge/databases/{kb_id}/export` | GET | admin | READ | 导出数据 |
| `/knowledge/databases/{kb_id}/folders` | POST | admin | MANAGE | 创建文件夹 |
| `/knowledge/databases/{kb_id}/documents/{doc_id}/move` | PUT | admin | MANAGE | 移动文件/文件夹 |
| `/knowledge/databases/{kb_id}/documents` | GET | admin | READ | 文档列表（目录/分页） |
| `/knowledge/databases/{kb_id}/documents/search` | GET | admin | READ | 按文件名搜索 |
| `/knowledge/databases/{kb_id}/documents/exists` | GET | admin | READ | 文件存在性检查 |
| `/knowledge/databases/{kb_id}/documents` | POST | admin | MANAGE | 添加文档（异步） |
| `/knowledge/databases/{kb_id}/documents/add` | POST | admin | MANAGE | 入库已上传文档 |
| `/knowledge/databases/{kb_id}/documents/parse` 等 4 个批量接口 | POST | admin | MANAGE | 批量解析/入库 |
| `/knowledge/databases/{kb_id}/documents/{doc_id}` | GET | admin | READ | 文档详情 |
| `/knowledge/databases/{kb_id}/documents/{doc_id}/basic` | GET | admin | READ | 文档基础信息 |
| `/knowledge/databases/{kb_id}/documents/{doc_id}/content` | GET | admin | READ | 文档内容 |
| `/knowledge/databases/{kb_id}/documents/{doc_id}/download` | GET | admin | READ | 下载原始文件 |
| `/knowledge/databases/{kb_id}/documents/{doc_id}` | DELETE | admin | MANAGE | 删除文档 |
| `/knowledge/databases/{kb_id}/documents/batch` | DELETE | admin | MANAGE | 批量删除 |
| `/knowledge/databases/{kb_id}/query` | POST | admin | READ | 查询知识库 |
| `/knowledge/databases/{kb_id}/query-test` | POST | admin | READ | 测试查询 |
| `/knowledge/databases/{kb_id}/query-params` | GET/PUT | admin | READ/MANAGE | 查询参数配置 |
| `/knowledge/databases/{kb_id}/graph-build/config` | POST | admin | MANAGE | 配置/锁定图谱抽取 |
| `/knowledge/databases/{kb_id}/graph-build/index` | POST | admin | MANAGE | 执行图谱构建 |
| `/knowledge/databases/{kb_id}/graph-build/status` | GET | admin | READ | 构建状态 |
| `/knowledge/databases/{kb_id}/graph-build/failed-chunks` | GET | admin | READ | 失败 Chunk 样例 |
| `/knowledge/databases/{kb_id}/graph-build/reset` | POST | admin | MANAGE | 重置构建状态 |
| `/knowledge/databases/{kb_id}/graph-build/reconcile` | POST | admin | MANAGE | 修复向量索引 |

## 使用示例

### 示例 1：全局公开

所有人可读，仅创建者可管理：

```json
{
  "version": 2,
  "read_scope": {"access_level": "global"},
  "manage_scope": {"access_level": "user", "user_uids": ["creator_uid"]}
}
```

### 示例 2：部门内可见

指定部门可读，部门管理员可管理：

```json
{
  "version": 2,
  "read_scope": {"access_level": "department", "department_ids": [5]},
  "manage_scope": {"access_level": "user", "user_uids": ["admin_uid"]}
}
```

### 示例 3：仅指定用户

仅授权用户可访问：

```json
{
  "version": 2,
  "read_scope": {"access_level": "user", "user_uids": ["uid_001", "uid_002"]},
  "manage_scope": {"access_level": "user", "user_uids": ["uid_001"]}
}
```

### 示例 4：修改已有知识库的权限

```bash
curl -X PUT http://<host>/api/knowledge/databases/kb_xxx \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "产品知识库",
    "description": "产品手册与 FAQ",
    "share_config": {
      "version": 2,
      "read_scope": {"access_level": "department", "department_ids": [1, 2], "user_uids": []},
      "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": ["uid_001"]}
    }
  }'
```

### 示例 5：配置并执行图谱构建

```bash
# 1. 锁定抽取配置（LLM 抽取器 + Schema 约束）
curl -X POST http://<host>/api/knowledge/databases/kb_xxx/graph-build/config \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{
    "extractor_type": "llm",
    "extractor_options": {
      "model_spec": "gpt-4o",
      "schema": {"entity_types": ["人物", "机构"], "relation_types": ["任职于"]},
      "concurrency_count": 4
    }
  }'

# 2. 提交构建任务
curl -X POST http://<host>/api/knowledge/databases/kb_xxx/graph-build/index \
  -H "Authorization: Bearer <token>"

# 3. 轮询状态
curl http://<host>/api/knowledge/databases/kb_xxx/graph-build/status \
  -H "Authorization: Bearer <token>"
```

## 租户知识库管理 API（Tenant 平台对接指南）

Tenant 服务（租户后台界面）通过本组 API 接管知识库的**目录管理、文档管理与权限控制**。前缀 `/api/tenant/knowledge`。

### 鉴权与响应约定

- **请求头**：`X-Tenant-Admin-Key`（与 Yuxi 环境变量 `TENANT_ADMIN_API_KEY` 比对，密钥由双方线下约定）+ `X-Tenant-Id`（目标租户 ID，int）。
- **响应约定**：与 AGENT_CLIENT_API.md 的统一 `{code,msg,data}` 包装**不同**，本组 API 直接用 HTTP 状态码表达结果：成功返回资源对象或 `{"message": ...}`，失败返回 `{"detail": "错误说明"}`。
- **严格租户隔离**：所有接口按 `X-Tenant-Id` 隔离——创建/查询自动绑定租户，跨租户访问一律 404 且不暴露资源存在性；资源 ID 由前端传入但服务端一律按租户校验，不信任前端。
- **权限模型**：权限**仅通过 `share_config` 表达**（创建/更新知识库接口），**没有独立的权限接口**；本 API 拥有该租户知识库的完整管理能力（租户管理员语义），员工的文档级 MANAGE 见「员工端点权限一览」。

### 错误码矩阵

| 场景 | 状态码 | detail 示例 |
|------|--------|-------------|
| Key 缺失或与配置不一致 | 401 | `无效的管理 API Key` |
| SaaS 未启用 / `TENANT_ADMIN_API_KEY` 未配置 | 404 | `Not Found` |
| 资源不存在 / 跨租户访问 | 404 | `知识库 {kb_id} 不存在` |
| 缺少 `X-Tenant-Id` 请求头 | 422 | `缺少 X-Tenant-Id 请求头` |
| `X-Tenant-Id` 非数字 | 422 | （FastAPI 类型校验） |
| 知识库名称重复 | 409 | `知识库名称 'xxx' 已存在，请使用其他名称` |
| 目录非空时删除 | 409 | `目录不为空，无法删除（请先删除其中的子目录与文档）` |
| 目录/文件不存在或不属于该知识库 | 400 / 404 | `目录 {folder_id} 不存在或不属于该知识库` |
| share_config 校验失败（越界 manage_scope 等） | 400 | 校验错误说明 |

### 知识库 CRUD

#### 创建知识库

**接口**：`POST /api/tenant/knowledge/databases`

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `database_name` | string | 是 | 知识库名称（租户内唯一） |
| `description` | string | 是 | 描述 |
| `embedding_model_spec` | string | 否 | 嵌入模型 spec |
| `kb_type` | string | 否 | 类型，默认 `milvus` |
| `llm_model_spec` | string | 否 | LLM 模型 spec |
| `additional_params` | object | 否 | 附加参数 |
| `share_config` | object | 否 | KB 级共享配置；缺省为租户内全局可读/全局可管理 |

**响应**：201 + 知识库对象（含 `kb_id`、`tenant_id`，字段见「知识库对象公共字段」）。

#### 知识库列表 / 详情 / 更新 / 删除

| 接口 | 说明 | 请求要点 | 响应要点 |
|------|------|----------|----------|
| `GET /databases` | 本租户知识库列表 | — | `{"databases": [知识库对象]}` |
| `GET /databases/{kb_id}` | 详情 | 跨租户 404 | 知识库对象 |
| `PUT /databases/{kb_id}` | 更新信息与 KB 级权限 | 见下 | `{"message": "更新成功", "database": 知识库对象}` |
| `DELETE /databases/{kb_id}` | 删除 | — | `{"message": "知识库已删除"}` |

**PUT 请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 名称 |
| `description` | string | 是 | 描述 |
| `llm_model_spec` | string \| null | 否 | 仅当非 null 时更新 |
| `additional_params` | object \| null | 否 | 仅当非 null 时更新 |
| `share_config` | object \| null | 否 | 仅当非 null 时更新（**KB 级权限唯一入口**） |

### 目录管理

| 接口 | 说明 | 请求体 | 响应 |
|------|------|--------|------|
| `POST /databases/{kb_id}/folders` | 创建目录 | `{"folder_name": "财务", "parent_id": null}`（`parent_id` 可选，null=根目录） | 目录对象（含 `file_id`） |
| `PUT /databases/{kb_id}/folders/{folder_id}/rename` | 重命名目录 | `{"folder_name": "新名称"}`（非空字符串） | `{"message": "目录已重命名", "file_id": ..., "filename": ...}` |
| `PUT /databases/{kb_id}/folders/{folder_id}/move` | 移动目录 | `{"new_parent_id": "fld-xxx"}`（null 或不传=移到根） | 移动后的目录元数据 |
| `DELETE /databases/{kb_id}/folders/{folder_id}` | 删除目录（**仅允许空目录**） | — | `{"message": "目录已删除"}`；有子目录或文档时 409，**不递归删除** |

### 文档管理

Tenant 管理端只负责**元数据管理与删除**，文档录入（上传/解析/入库）与正文/原始文件下载由 Yuxi 管理端或员工链路完成，本组 API 不提供。

| 接口 | 说明 | 响应 |
|------|------|------|
| `GET /databases/{kb_id}/documents?parent_id=&status=&page=&page_size=` | 分页文档列表（含文件夹行） | 分页结构（`items`/`total`/`page`/`page_size`/`has_more`） |
| `GET /databases/{kb_id}/documents/{file_id}/basic` | 文档元数据（不含正文） | 文件基础信息对象 |
| `DELETE /databases/{kb_id}/documents/{file_id}` | 删除文档（目标为目录时同样仅允许空目录，否则 409） | `{"message": "删除成功"}` / `{"message": "目录已删除"}` |

### 权限接管方式（share_config）

角色勾选由 **Tenant 后台自行展开为员工列表**，再通过 `share_config` 落到 Yuxi：

1. 租户后台勾选「哪些员工/角色可访问」→ Tenant 展开为员工 `employeeCode` 列表 → 写入 `read_scope.user_uids`（或 department/global 语义）；
2. 勾选「可编辑」→ 展开后写入 `manage_scope.user_uids`（**空列表合法，表达无员工编辑权**）；
3. `POST /databases` 或 `PUT /databases/{kb_id}` 提交；服务端校验 manage_scope ⊆ read_scope、维度不混用。

**员工标识**：`user_uids` 使用员工的 **`employeeCode`**（SaaS 员工登录 Yuxi 时本地 `users.uid = employeeCode`，同一套标识；不能使用 Tenant 数据库自增的 employee_id）。

### 推荐接管流程

1. **准备**：Yuxi 侧确认 `SAAS_ENABLED=true` 并配置 `TENANT_ADMIN_API_KEY`（与 Tenant 侧保存一致）。
2. **建库**：`POST /databases` 创建知识库，同时把展开好的 `share_config` 一并提交。
3. **建目录**：`POST /databases/{kb_id}/folders` 建目录树（rename/move 调整；删除只允许空目录）。
4. **文档**：`GET /databases/{kb_id}/documents` 分页浏览（Tenant 不缓存全量目录树），`basic` 查元数据，`DELETE` 删除。
5. **员工侧**：员工登录 Yuxi 后按 `share_config` 生效——read 命中可检索全库；manage 命中可在前端/Agent 上传与删除文档（KB 结构操作仍 403）。

## 员工端点权限一览（SaaS）

权限完全由 KB 级 `share_config` 决定，目录仅作组织不分权限：

| 端点 | 员工行为 |
|------|----------|
| `GET /databases/accessible` | 仅本租户 + read_scope 命中的知识库 |
| `GET /databases/{kb_id}` | KB 级 READ + 本租户；敏感参数（密钥等）对员工始终脱敏 |
| `GET /databases/{kb_id}/documents`、`/search` | KB 级 READ 即可查看全库（无目录过滤） |
| `POST /databases/{kb_id}/query`、`/query-test` | KB 级 READ 即可检索全库 |
| `POST /files/upload` | 必须带 `kb_id` 且 KB 级 MANAGE（manage_scope 命中） |
| `POST /databases/{kb_id}/documents/add` | KB 级 MANAGE（`parent_id` 仅组织语义） |
| `POST /documents/parse`、`/documents/index` | KB 级 MANAGE |
| `DELETE /documents/{doc_id}`、`/documents/batch` | KB 级 MANAGE；**文件夹目标一律 403** |
| 目录增删改、move、KB CRUD、share_config 修改、图谱构建等 | **员工一律 403**（管理员门槛，与员工 MANAGE 无关） |
| Agent 工具 | `upload_kb_file`/`delete_kb_file` 按 KB 级 MANAGE 判定；检索无目录过滤 |

## 代码位置

| 模块 | 文件路径 |
|------|----------|
| 权限模型 | `backend/package/yuxi/permissions/resource_permission.py` |
| 权限校验适配 | `backend/server/utils/knowledge_permissions.py` |
| 员工权限依赖（KB 级 MANAGE 边界） | `backend/server/utils/employee_kb_permissions.py` |
| 租户管理 API | `backend/server/routers/tenant_knowledge_router.py` |
| 知识库管理器 | `backend/package/yuxi/knowledge/manager.py` |
| 知识库路由 | `backend/server/routers/knowledge_router.py` |
| 图谱构建服务 | `backend/package/yuxi/knowledge/graphs/milvus_graph_service.py` |
| 图谱抽取器 | `backend/package/yuxi/knowledge/graphs/extractors/`（`llm.py`、`factory.py`） |
| 响应序列化 | `backend/server/utils/knowledge_response.py` |
