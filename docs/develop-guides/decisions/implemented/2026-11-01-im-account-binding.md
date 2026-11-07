# IM 账号绑定

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/im_binding_service.py

## 问题

IM 机器人用「机器人归属人」的 API Key 执行所有消息：同一机器人下任何飞书/钉钉用户都以归属人身份运行，线程、知识库、附件和历史全部共享。`allowed_users` 只能拦人，不能表达「这条消息属于哪个 Yuxi 用户」。员工与 Yuxi 账号是一对一关系，需要把飞书/钉钉身份稳定映射到自己的 Yuxi 账号，使 IM 消息按本人权限与线程执行。

## 决策

绑定关系由 Tenant 的 `employee/channel-bindings` 唯一保存，Yuxi 不再持久化第二份映射。绑定入口在 Yuxi 个人设置内，用该平台唯一启用的机器人凭据发起飞书/钉钉扫码授权，回调换取平台身份后调用 Tenant 保存；消息侧用一份 TTL 与 Tenant AgentSession 对齐的 Redis 缓存避免逐条调用 Tenant，缓存未命中时才回查 Tenant。

### 实现方案

**持久化与缓存**：

- `im_binding_sessions(id, user_id, platform, bot_id, state_hash, status, expires_at, used_at)`：绑定二维码背后的一次性 state，只存 sha256 哈希，10 分钟有效期，用后置 `used_at`，重放必须失败。
- Tenant 的 `POST/GET/DELETE /api/v1/agent/inner/employee/channel-bindings` 与 `POST /api/v1/agent/inner/auth/channel` 是绑定事实 Owner，契约见团队内部的 Agent 频道绑定接口文档。绑定、解绑与查询使用 `Authorization: AgentSession <token>`；平台账号登录使用 `X-Tenant-Admin-Key`。
- Redis 映射：正向 `saas:im-binding:{platform}:{channel_user_id} → uid`，反向 `saas:im-binding:uid:{uid}:{platform} → channel_user_id`；TTL 取 Tenant AgentSession 剩余秒数。缓存只保存 uid 与平台用户 ID，不保存 Token、密钥或平台资料。
- `im_bot_configs.require_binding`（布尔，默认 `false`）：过渡期开关。关闭时未绑定身份回退到机器人归属人并附提示，打开后未绑定身份直接拦截。
- 历史库中已存在的 `im_account_bindings` 表不再被读取或写入，模型与建表 DDL 已移除；Yuxi 不自动删表，运维确认无消费者后可手工清理。

**绑定链路**（入口在 Yuxi 个人设置，`web/src/components/ImAccountBindingComponent.vue` 负责前端交互）：

1. `POST /api/user/im-bindings/{platform}/sessions`：要求有效的 Tenant AgentSession（缺失 401 `im_binding_login_required`），取该平台唯一启用的机器人凭据生成 state，返回 `{bind_url, expires_at}`；前端用 `bind_url` 渲染二维码（`qrcode` 前端库，后端不存图片）。同一平台多个启用机器人时报错要求收敛，不静默挑选凭据。非 SaaS 部署返回 400 `im_binding_not_enabled`。
2. `GET /api/im-bindings/callback/{platform}?code=&state=`（公开路由，靠 state 鉴权）：用 `UPDATE ... WHERE pending AND used_at IS NULL AND expires_at > now RETURNING` 原子消费 state，并发回调最多一个成功 → 换 user access token → 取平台身份（飞书 `open_id`；钉钉 `unionId` 经 `topapi/v2/user/getbyunionid` 换成消息侧 `userid`）→ 调用 Tenant 绑定。Tenant 成功后才写 Redis 并返回成功结果页；Tenant 失败时映射为明确错误，不返回成功。
3. `GET /api/user/im-bindings` 实时读取 Tenant 三个平台的绑定，只向前端暴露已绑定的飞书/钉钉；`DELETE /api/user/im-bindings/{platform}` 由 Tenant 解绑并清理本地缓存。三个用户接口都要求有效的 Tenant AgentSession，缺失返回 401 `im_binding_login_required`。

Tenant 错误码映射：`20316`（平台账号已绑其他员工）→ 409 `im_account_already_bound`；`20314`（员工会话失效）→ 清 Redis AgentSession 与绑定缓存后 401 `im_binding_login_required`；`20302`（员工不存在/停用）→ 403 `im_employee_unavailable`；`20317`（管理密钥错误）或其他失败 → 502 `im_binding_tenant_unavailable`。

**登录与登出**：SaaS 登录成功后在 `_save_saas_login_state` 中无条件保存 Tenant AgentSession（即使 Tenant 没有下发 MCP Runtime），随后调用一次 `GET channel-bindings` 预热缓存：对飞书/钉钉先清旧映射再写当前绑定，避免解绑或换号后继续命中旧缓存。预热失败只记告警，不影响登录。登出在清理 AgentSession 后删除该用户全部绑定缓存。

**消息链路**：`backend/server/routers/agent_invocation_channel_router.py` 保留「API Key = 机器人归属人」的传输身份，IM 渠道只接受机器人自身的 API Key：`AuthPrincipal.api_key_id` 必须等于 `im_bot_configs.api_key_id`，JWT 登录态（包括机器人归属人）返回 403 `im_channel_api_key_required`，同属主但非该机器人的 Key 返回 403 `im_bot_credential_mismatch`。随后 `resolve_channel_identity` 解析真实用户，以该用户身份提交 Run（线程、权限、模型配置、附件全按该用户走），并在成功响应返回 `identity_source`。同一解析对 `/resume` 与 slash command 生效；非 IM 渠道（如 `yuxi-cli`）保持原有 JWT/API Key 认证与响应结构，不进入绑定解析。

身份解析顺序：

1. 非 SaaS 部署直接以机器人归属人执行，`require_binding` 不生效，也不调用 Tenant；响应 `identity_source=owner`，网关不附绑定提示。
2. 缓存命中且本地用户仍存在时直接解析，Tenant 调用次数为 0。
3. 缓存未命中时调用一次 `auth/channel`，用 `employeeCode` 作为本地 uid 经 `find_or_create_saas_user` 同步用户，再按 AgentSession 剩余时间回填缓存。
4. Tenant 返回 `20206`（未绑定或员工停用）时按 `require_binding` 分流：打开返回 409 `im_account_not_bound`，关闭以归属人执行并带 `identity_source=owner_fallback`。
5. Tenant 不可达或返回其他错误时返回 502 `im_binding_unavailable`，不静默降级为归属人。

绑定后 Run 的 `uid` 是绑定用户，而网关仍只持有机器人归属人的 API Key。运行事件流与线程产物下载在原有「Run / 线程属于调用方」校验之外增加一条 Channel 读权限：Run 来源为 `channel`，其 `origin_metadata.account_id` 解析到仍启用且归属调用方的机器人时允许读取；非 Channel Run、机器人缺失、停用或归属他人一律不解锁。该规则集中实现在 `im_binding_service.can_read_channel_run_as_bot_owner` 与 `can_read_channel_thread_as_bot_owner`，分别供 `agent_run_service` 事件流与 `artifact_service` 产物下载复用。

Owner 分工：`im_binding_service.py` 拥有绑定会话、平台身份换取、Tenant 绑定调用与消息身份解析；`saas_client.py` 拥有 Tenant 四接口的请求与响应校验；`im_binding_cache.py` 拥有缓存键、TTL 与清理；`agent_invocation_channel_router.py` 拥有消息入口的身份解析接入与传输身份校验；`auth_router.py` 拥有登录会话保存、预热与登出清理；`ImAccountBindingComponent.vue` 拥有个人设置页入口与二维码交互；`packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py` 拥有 `owner_fallback` 与 `im_account_not_bound` 的提示文案。

## 替代方案

- **Yuxi 本地保存 `im_account_bindings`**：绑定只写 Yuxi、消息只查本地；代价是与 Tenant 的 `employee/channel-bindings` 形成两份可独立漂移的事实，Tenant 侧解绑或换号无法被 Yuxi 感知。已移除。
- **每条 IM 消息都调用 Tenant `auth/channel`**：实现简单且始终以 Tenant 为准；代价是每条消息增加一次跨服务调用与故障面，Tenant 抖动会直接阻断全部 IM 消息。改为登录预热与命中缓存，未命中才回查。
- **缓存不设 TTL 或本地长期保存**：消息侧最快；代价是解绑、换号和员工停用无法及时生效，且重新引入本地事实。缓存 TTL 与 AgentSession 对齐，登录时重建。
- **把飞书/钉钉做成 SSO 登录方式**：扫码即登录并顺带建立映射；代价是登录链路、会话与用户开通策略同时改动，绑定语义与登录语义混在一起，风险最高。

## 后果

- 绑定、解绑与查询依赖 Tenant 可达；Tenant 不可用时明确失败（401/502），不伪装成功。
- 消息侧在缓存命中时不访问 Tenant；缓存未命中、过期或 Redis 故障时每条消息最多回查一次 Tenant。缓存写入失败不回滚 Tenant 已提交的绑定。
- 过渡期硬切换：`require_binding` 默认关闭，先上线绑定能力与归属人回退提示；使用该机器人的同事完成绑定后，由管理员逐个机器人打开开关，硬切后未绑定身份被拦截。非 SaaS 部署忽略该开关，始终按机器人归属人执行。
- 线程不连续：绑定改变 uid 后 `channel_*` 线程 ID 随之变化，绑定前的 IM 会话历史不续接；如需迁移旧会话须单独立项。
- 一个平台同时启用多个机器人时绑定会话直接报错，要求管理员收敛。
- 钉钉 `unionId → userid` 依赖通讯录权限，换取失败即整次绑定失败并回明确提示，不做降级匹配。
- 回调地址取自创建会话时的请求 `base_url`，要求该域名手机侧可达且已在平台后台登记。
- `allowed_users` 白名单继续作为入口门禁，与绑定相互独立。
- Channel Run 的读权限跟随机器人启停：机器人停用后，归属人不再能通过该规则读取其历史 Channel Run 事件与产物。
- 历史库的 `im_account_bindings` 成为无消费者残留表；新库不创建，旧库需人工确认后清理。
- 过期 `im_binding_sessions` 行不会主动删除；条件 `UPDATE` 保证其不可再用，运维可按 `expires_at` 归档清理。
- 缓存冷启动回查只同步本地用户，不重写员工上下文；绑定用户必然先完成过 SaaS 登录，`UserConfig` 与部门信息已存在。

## 验证

本槽位真实执行的命令与结果：

- `docker compose exec -T api python -m pytest test/unit -m "not slow" -q --deselect test/unit/routers/test_document_file_exists_route.py::test_document_file_exists_route_accepts_filename_with_slashes` → **2752 passed, 58 skipped**。该用例在既有代码上会挂死，本槽位未纳入全量运行。
- `docker compose exec -T api python -m pytest test/unit/routers/test_agent_invocation_channel_router.py test/unit/services/test_artifact_service.py -q` → **35 passed**。
- `docker compose exec -T api python -m pytest test/integration/services/test_channel_run_read_access.py -q` → **2 passed**。
- `docker compose exec -T api python -m pytest test/unit/routers/test_auth_router_saas.py test/unit/routers/test_im_binding_router.py test/unit/routers/test_agent_invocation_channel_router.py test/unit/services/test_im_binding_service.py test/unit/services/test_im_binding_cache.py test/unit/server/test_saas_session_auth.py -q` → **33 passed**。
- `docker compose exec -T api python -m pytest test/integration/storage/test_im_binding_schema.py test/integration/services/test_im_binding_service.py test/integration/api/test_channel_identity_binding.py -q` → **34 passed**（真实 PostgreSQL + Redis，含 Tenant 错误码映射、state 并发原子消费、缓存命中零调用、Redis 读失败回退、冷启动回查、归属人回退、非 SaaS `identity_source=owner`、机器人 API Key 校验与失败关闭）。
- `docker compose exec -T api python -m pytest test/integration/services/test_channel_run_read_access.py test/integration/api/test_im_binding_router.py test/integration/api/test_agent_invocation_channel_api.py test/integration/api/test_auth_router.py -q` → **7 passed, 14 skipped, 2 failed**。失败的两条是 `test_auth_router.py` 的本地登录断言：本槽位 `SAAS_ENABLED=true` 且 Tenant 不可达，登录走 Tenant 后返回 502，而用例预期本地 401/限流，与本次变更无关。
- `python3 -m ruff check --line-length 120 --select F,E,W,UP <本次修改的 Python 文件>` → **All checks passed**。`manager.py` 本次只删除 DDL，未新增超长行；该文件 1573/1625/1626/1671/1719 的 5 处既有 E501 不在本次范围，未顺手修改。
- `cd packages/yuxi-im-gateway && .venv/bin/python -m pytest tests -q` → **66 passed**。
- `cd web && node --test --test-concurrency=1 "test/**/*.test.js" "test/**/*.spec.js"` → **490 passed**；`./node_modules/.bin/eslint . --max-warnings=0` → **exit 0**。
- `python3 -m unittest scripts.test_verify_engineering_contracts` → **63 tests OK**。
- `python3 scripts/verify_engineering_contracts.py` → **工程信任检查通过（63 decisions / 5 workflows / 4 agents files / 221 docs / 36 routers / 278 web sources）**。
- `cd docs && ./node_modules/.bin/vitepress build` → **构建成功**。

未验证（本槽位无凭据或依赖外部环境）：

- **真实飞书/钉钉扫码与 Tenant 联调**：需有测试应用凭据与可达的 Tenant 环境，本槽位未配置，未验证绑定/解绑/登录的真实网络闭环。
- **E2E 用例 `backend/test/e2e/test_im_binding_flow.py`**：用预置 Redis 缓存模拟 Tenant 已绑定身份，验证两条不同 `sender_id` 分别落库到两个 uid 的 `channel_*` 线程与强绑拦截；本槽位未配置 `E2E_USERNAME`/`E2E_PASSWORD`，运行时跳过。
- **基于 HTTP 凭据的集成用例**（如 `test_im_binding_router.py`、`test_agent_invocation_channel_api.py`）：本槽位未配置管理员凭据，跳过。
- **Docker web 镜像内 `pnpm build`**：镜像 node_modules 早于 `qrcode` 依赖提交；宿主依赖与代码未变，本槽位未重建镜像。
