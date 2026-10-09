# Platform 与 Tenant 内部接口文档

## 1. 文档范围

本文档基于以下代码整理：

- `platform` 服务：`TenantInnerController`、`BaseInnerController`
- `tenant` 服务：`AgentInnerController`

这里只覆盖两个服务之间以及 MCPServer 调用使用的“内部接口”，不包含后台管理接口和租户前台接口。

## 2. 通用约定

### 2.1 Platform 与 Tenant 大部分接口的统一响应结构

除 `tenant` 服务的 `GET /tenant/employee_permission` 外，其余内部接口都使用统一响应体：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {}
}
```

字段说明：

| 字段     | 类型                      | 含义                         |
| ------ | ----------------------- | -------------------------- |
| `code` | `int`                   | 响应状态码，`0` 表示成功，非 `0` 表示失败。 |
| `msg`  | `string`                | 响应提示信息；成功时固定为 `ok`。        |
| `data` | `object / array / null` | 业务数据；无返回内容时通常为 `null`。     |

### 2.2 时间字段格式

文档中的 `LocalDateTime` 字段由 Spring 默认序列化，通常表现为 ISO-8601 格式，例如：

```json
"2026-08-10T15:30:00"
```

### 2.3 状态字段约定

不同服务中的状态值含义不同，调用方需要区分：

| 字段        | 服务                | 取值                     | 含义                         |
| --------- | ----------------- | ---------------------- | -------------------------- |
| `status`  | `platform` 租户状态   | `1` / `2`              | `1` 表示启用，`2` 表示禁用。         |
| `enabled` | `tenant` 员工/规则状态  | `1` / `0`              | `1` 表示启用，`0` 表示停用。         |
| `status`  | `tenant` 员工权限快照响应 | `ENABLED` / `DISABLED` | 根据员工 `enabled` 字段转换后的文本状态。 |

## 3. Platform 服务内部接口

服务代码位置：

- `D:\Codes\Origin-Lightyear\saas\server\platform\src\main\java\com\kevin\saas\controller\tenant\TenantInnerController.java`
- `D:\Codes\Origin-Lightyear\saas\server\platform\src\main\java\com\kevin\saas\controller\tenant\BaseInnerController.java`

### 3.1 校验租户管理员登录

- 方法：`POST`
- 路径：`/inner/tenant/login-check`
- 认证要求：`@AllowAnonymous`
- 用途：校验租户登录手机号和密码，并返回租户主数据，供 `tenant` 服务完成登录准入。

请求体：

```json
{
  "mobile": "13800138000",
  "pwd": "12345678"
}
```

请求字段说明：

| 字段       | 类型       | 必填  | 含义                                       |
| -------- | -------- | --- | ---------------------------------------- |
| `mobile` | `string` | 是   | 租户登录手机号，同时也是平台侧用于识别租户身份的账号。要求为标准大陆手机号格式。 |
| `pwd`    | `string` | 是   | 租户登录原始密码，由 Platform 内部完成密码校验。            |

成功响应：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "id": 1,
    "name": "测试租户",
    "linkman": "张三",
    "mobile": "13800138000",
    "planId": 2,
    "empNum": 100,
    "status": 1,
    "authEndTime": "2026-12-31T23:59:59"
  }
}
```

`data` 字段说明：

| 字段            | 类型                 | 含义                                |
| ------------- | ------------------ | --------------------------------- |
| `id`          | `long`             | 平台租户主键，后续所有跨服务租户关联都以该值为准。         |
| `name`        | `string`           | 租户公司名称。                           |
| `linkman`     | `string`           | 租户联系人姓名。                          |
| `mobile`      | `string`           | 租户登录手机号。                          |
| `planId`      | `long`             | 当前生效的套餐 ID。                       |
| `empNum`      | `int`              | 当前购买的员工上限；`tenant` 服务会据此控制启用员工数量。 |
| `status`      | `int`              | 租户状态；`1` 表示启用，`2` 表示禁用。           |
| `authEndTime` | `string(datetime)` | 租户授权截止时间，影响登录和服务可用性判断。            |

### 3.2 查询租户内部配置

- 方法：`GET`
- 路径：`/inner/tenant/config/{id}`
- 认证要求：`@AllowAnonymous`
- 用途：按租户 ID 读取 LLM 与 MCP 相关配置，供 `tenant` 服务拉取平台侧租户配置。

路径参数说明：

| 参数   | 类型     | 必填  | 含义       |
| ---- | ------ | --- | -------- |
| `id` | `long` | 是   | 平台租户 ID。 |

成功响应：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "id": 1,
    "llmKey": "sk-xxx",
    "llmUrl": "https://example.com/v1",
    "mcpUrl": "https://tenant-mcp.example.com",
    "status": 1,
    "empNum": 100,
    "authEndTime": "2026-12-31T23:59:59",
    "version": 8
  }
}
```

`data` 字段说明：

| 字段            | 类型                 | 含义                                              |
| ------------- | ------------------ | ----------------------------------------------- |
| `id`          | `long`             | 平台租户 ID。                                        |
| `llmKey`      | `string`           | 租户调用大模型服务时使用的专属密钥。                              |
| `llmUrl`      | `string`           | 租户大模型服务基础地址。                                    |
| `mcpUrl`      | `string`           | 租户 MCP 服务地址，供内部系统按租户路由调用。                       |
| `status`      | `int`              | 租户状态；`1` 表示启用，`2` 表示禁用。                         |
| `empNum`      | `int`              | 员工上限。                                           |
| `authEndTime` | `string(datetime)` | 授权截止时间。                                         |
| `version`     | `int`              | Platform 维护的租户配置版本号；权限、员工、部门等变更后会递增，用于下游感知配置更新。 |

### 3.3 递增租户配置版本

- 方法：`POST`
- 路径：`/inner/tenant/{id}/version`
- 认证要求：`@AllowAnonymous`
- 用途：通知 Platform 将指定租户的配置版本号加一，供下游缓存或配置同步机制感知变更。

路径参数说明：

| 参数   | 类型     | 必填  | 含义       |
| ---- | ------ | --- | -------- |
| `id` | `long` | 是   | 平台租户 ID。 |

请求体：无

成功响应：

```json
{
  "code": 0,
  "msg": "ok",
  "data": null
}
```

### 3.4 查询 NewAPI 全局配置

- 方法：`GET`
- 路径：`/inner/base/newapi/config`
- 认证要求：`@AllowAnonymous`
- 用途：读取 Platform 统一维护的 NewAPI 全局配置，供 `tenant` 服务访问下游 NewAPI。

成功响应：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "baseUrl": "https://newapi.example.com",
    "key": "downstream-key"
  }
}
```

`data` 字段说明：

| 字段        | 类型       | 含义                                 |
| --------- | -------- | ---------------------------------- |
| `baseUrl` | `string` | NewAPI 基础地址。                       |
| `key`     | `string` | Platform 下发给 `tenant` 服务使用的下游鉴权密钥。 |

## 4. Tenant 服务内部接口

服务代码位置：

- `D:\Codes\Origin-Lightyear\saas\server\tenant\src\main\java\com\kevin\saas\controller\permission\AgentInnerController.java`

这些接口主要供 MCPServer 或其他内部调用方使用。

### 4.1 查询员工有效权限快照

- 方法：`GET`
- 路径：`/tenant/employee_permission`
- 认证要求：`@AllowAnonymous`
- 用途：按租户和员工读取当前有效权限快照，供 MCPServer 做权限判断。
- 特别说明：该接口**不使用** `ApiResult` 包装，直接返回 JSON 对象。

查询参数说明：

| 参数           | 类型     | 必填  | 含义        |
| ------------ | ------ | --- | --------- |
| `tenantId`   | `long` | 是   | 平台租户 ID。  |
| `employeeId` | `long` | 是   | 租户下员工 ID。 |

成功响应示例：

```json
{
  "employeeId": "1001",
  "status": "ENABLED",
  "versionNumber": 1754812800,
  "departmentInfo": {
    "departmentName": "研发部"
  },
  "permissionConfig": {
    "tenantId": 1,
    "employeeId": 1001,
    "employeeCode": "E1001",
    "employeeName": "李四",
    "departmentId": 10,
    "departmentName": "研发部",
    "roles": [],
    "denyRules": [],
    "sensitiveRules": [],
    "ruleVersion": 1754812800,
    "enabled": 1
  }
}
```

顶层字段说明：

| 字段                 | 类型       | 含义                                                                              |
| ------------------ | -------- | ------------------------------------------------------------------------------- |
| `employeeId`       | `string` | 员工 ID 的字符串形式，供下游系统直接透传使用。                                                       |
| `status`           | `string` | 员工状态文本；当员工 `enabled=1` 时返回 `ENABLED`，否则返回 `DISABLED`。                           |
| `versionNumber`    | `long`   | 权限版本号，对应 `permissionConfig.ruleVersion`；实际值为最近一次权限相关数据更新时间的秒级时间戳，若从未变更则默认为 `1`。 |
| `departmentInfo`   | `object` | 员工部门简要信息。                                                                       |
| `permissionConfig` | `object` | 员工完整权限快照。                                                                       |

`departmentInfo` 字段说明：

| 字段               | 类型       | 含义                    |
| ---------------- | -------- | --------------------- |
| `departmentName` | `string` | 员工所属部门名称；没有部门时返回空字符串。 |

`permissionConfig` 字段说明：

| 字段               | 类型              | 含义                              |
| ---------------- | --------------- | ------------------------------- |
| `tenantId`       | `long`          | 平台租户 ID。                        |
| `employeeId`     | `long`          | 员工 ID。                          |
| `employeeCode`   | `string`        | 员工编码，便于和外部系统做身份映射。              |
| `employeeName`   | `string`        | 员工姓名。                           |
| `departmentId`   | `long`          | 员工所属部门 ID；未分配部门时可能为空。           |
| `departmentName` | `string`        | 员工所属部门名称。                       |
| `roles`          | `array<object>` | 员工当前生效的角色列表，仅返回启用状态的角色。         |
| `denyRules`      | `array<object>` | 员工当前命中的禁止规则列表，由角色绑定关系推导得出。      |
| `sensitiveRules` | `array<object>` | 员工当前生效的敏感规则列表，已剔除对该员工或其角色豁免的规则。 |
| `ruleVersion`    | `long`          | 权限规则版本号。                        |
| `enabled`        | `int`           | 员工启用状态，`1` 表示启用，`0` 表示停用。       |

`roles[]` 元素字段说明：

| 字段            | 类型                 | 含义                       |
| ------------- | ------------------ | ------------------------ |
| `id`          | `long`             | 角色 ID。                   |
| `tenantId`    | `long`             | 角色所属租户 ID。               |
| `roleName`    | `string`           | 角色名称。                    |
| `description` | `string`           | 角色说明，描述授权边界。             |
| `enabled`     | `int`              | 角色是否启用；正常情况下该列表只会返回启用角色。 |
| `deleted`     | `int`              | 逻辑删除标记。                  |
| `createTime`  | `string(datetime)` | 创建时间。                    |
| `updateTime`  | `string(datetime)` | 更新时间。                    |

`denyRules[]` 元素字段说明：

| 字段               | 类型                 | 含义                                      |
| ---------------- | ------------------ | --------------------------------------- |
| `id`             | `long`             | 禁止规则 ID。                                |
| `tenantId`       | `long`             | 规则所属租户 ID。                              |
| `ruleName`       | `string`           | 规则名称。                                   |
| `description`    | `string`           | 规则的自然语言描述。                              |
| `structuredJson` | `string`           | 由 LLM 结构化后的规则 JSON。                     |
| `readableText`   | `string`           | 面向用户的规则解释文本。                            |
| `enabled`        | `int`              | 规则是否启用。                                 |
| `deleted`        | `int`              | 逻辑删除标记。                                 |
| `roleIds`        | `array<long>`      | 规则绑定的角色 ID 列表；查询结果是否返回该字段取决于 Mapper 映射。 |
| `createTime`     | `string(datetime)` | 创建时间。                                   |
| `updateTime`     | `string(datetime)` | 更新时间。                                   |

`sensitiveRules[]` 元素字段说明：

| 字段               | 类型                 | 含义                           |
| ---------------- | ------------------ | ---------------------------- |
| `id`             | `long`             | 敏感规则 ID。                     |
| `tenantId`       | `long`             | 规则所属租户 ID。                   |
| `ruleName`       | `string`           | 规则名称。                        |
| `description`    | `string`           | 敏感数据识别规则的自然语言描述。             |
| `structuredJson` | `string`           | 由 LLM 生成的结构化规则 JSON。         |
| `readableText`   | `string`           | 面向用户展示的规则解释文本。               |
| `maskTemplate`   | `string`           | 脱敏模板编码，供 MCPServer 选择具体脱敏策略。 |
| `exemptionJson`  | `string`           | 原文查看豁免配置 JSON，记录可豁免的角色和员工。   |
| `enabled`        | `int`              | 规则是否启用。                      |
| `deleted`        | `int`              | 逻辑删除标记。                      |
| `createTime`     | `string(datetime)` | 创建时间。                        |
| `updateTime`     | `string(datetime)` | 更新时间。                        |

### 4.2 员工鉴权

- 方法：`POST`
- 路径：`/tenant/auth`
- 认证要求：`@AllowAnonymous`
- 用途：校验员工账号密码，并返回员工基本身份信息与 LLM 访问信息。

请求体：

```json
{
  "tenantId": 1,
  "mobile": "13800138000",
  "password": "Passw0rd"
}
```

请求字段说明：

| 字段         | 类型       | 必填  | 含义                    |
| ---------- | -------- | --- | --------------------- |
| `tenantId` | `long`   | 是   | 平台租户 ID，用于在对应租户下查找员工。 |
| `mobile`   | `string` | 是   | 员工登录手机号。              |
| `password` | `string` | 是   | 员工原始登录密码。             |

成功响应示例：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "employeeId": 1001,
    "employeeCode": "E1001",
    "employeeName": "李四",
    "departmentName": "研发部",
    "enabled": 1,
    "llmKey": "nk-xxx",
    "llmUrl": "https://newapi.example.com"
  }
}
```

`data` 字段说明：

| 字段               | 类型       | 含义                        |
| ---------------- | -------- | ------------------------- |
| `employeeId`     | `long`   | 员工 ID。                    |
| `employeeCode`   | `string` | 员工编码。                     |
| `employeeName`   | `string` | 员工姓名。                     |
| `departmentName` | `string` | 员工部门名称。                   |
| `enabled`        | `int`    | 员工启用状态，`1` 表示启用，`0` 表示停用。 |
| `llmKey`         | `string` | 该员工在 NewAPI 侧的 API Key。   |
| `llmUrl`         | `string` | NewAPI 基础地址。              |

### 4.3 查询员工 LLM 配置

- 方法：`POST`
- 路径：`/tenant/employee_llm_config`
- 认证要求：`@AllowAnonymous`
- 用途：读取指定员工可用的 LLM 配置。

请求体：

```json
{
  "tenantId": 1,
  "employeeId": 1001
}
```

请求字段说明：

| 字段           | 类型     | 必填  | 含义       |
| ------------ | ------ | --- | -------- |
| `tenantId`   | `long` | 是   | 平台租户 ID。 |
| `employeeId` | `long` | 是   | 员工 ID。   |

成功响应示例：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "apiKey": "nk-xxx",
    "newapiUrl": "https://newapi.example.com"
  }
}
```

`data` 字段说明：

| 字段          | 类型       | 含义                     |
| ----------- | -------- | ---------------------- |
| `apiKey`    | `string` | 该员工对应的 NewAPI API Key。 |
| `newapiUrl` | `string` | NewAPI 基础地址。           |

### 4.4 上报权限审计日志

- 方法：`POST`
- 路径：`/tenant/permission-audit`
- 认证要求：`@AllowAnonymous`
- 用途：接收 MCPServer 的权限审计结果，落库保存，便于后续追溯。

请求体示例：

```json
{
  "tenantId": 1,
  "employeeId": 1001,
  "requestMessage": "导出全部客户手机号",
  "planSummary": {
    "steps": [
      "查询客户表",
      "导出手机号"
    ]
  },
  "reviewResult": {
    "riskLevel": "high",
    "reason": "命中禁止规则"
  },
  "decision": "deny",
  "matchedRules": [
    {
      "ruleId": 11,
      "ruleName": "禁止导出客户手机号"
    }
  ],
  "modelVersion": "gpt-5-2026-08"
}
```

请求字段说明：

| 字段               | 类型                        | 必填  | 含义                                               |
| ---------------- | ------------------------- | --- | ------------------------------------------------ |
| `tenantId`       | `long`                    | 是   | 平台租户 ID。缺失时接口直接报参数错误。                            |
| `employeeId`     | `long`                    | 是   | 发起请求的员工 ID。缺失时接口直接报参数错误。                         |
| `requestMessage` | `string`                  | 否   | 员工原始请求内容，用于回溯当时让 MCP 执行的指令。                      |
| `planSummary`    | `object / array / string` | 否   | 执行计划摘要；如果传对象或数组，服务端会序列化成 JSON 字符串保存；为空时保存为 `{}`。 |
| `reviewResult`   | `object / array / string` | 否   | 权限审查结果；保存规则同 `planSummary`。                      |
| `decision`       | `string`                  | 是   | 审查决策，例如 `allow`、`deny`。                          |
| `matchedRules`   | `object / array / string` | 否   | 命中的规则摘要；保存规则同 `planSummary`。                     |
| `modelVersion`   | `string`                  | 否   | 参与审查的模型版本标识，便于后续比对模型升级前后的判断差异。                   |

成功响应：

```json
{
  "code": 0,
  "msg": "ok",
  "data": null
}
```

## 5. 调用关系简表

### 5.1 Tenant 调用 Platform

| 调用方                                    | 接口                                | 作用                   |
| -------------------------------------- | --------------------------------- | -------------------- |
| `PlatformTenantClient.loginCheck`      | `POST /inner/tenant/login-check`  | 校验租户登录，并同步拉取租户主数据。   |
| `PlatformTenantClient.getConfig`       | `GET /inner/tenant/config/{id}`   | 拉取租户平台配置。            |
| `PlatformTenantClient.getNewApiConfig` | `GET /inner/base/newapi/config`   | 拉取 NewAPI 全局配置。      |
| `PlatformTenantClient.bumpVersion`     | `POST /inner/tenant/{id}/version` | 通知 Platform 递增租户版本号。 |

### 5.2 MCPServer 调用 Tenant

| 调用方       | 接口                                 | 作用           |
| --------- | ---------------------------------- | ------------ |
| MCPServer | `GET /tenant/employee_permission`  | 获取员工权限快照。    |
| MCPServer | `POST /tenant/auth`                | 校验员工身份。      |
| MCPServer | `POST /tenant/employee_llm_config` | 获取员工 LLM 配置。 |
| MCPServer | `POST /tenant/permission-audit`    | 上报权限审计记录。    |
