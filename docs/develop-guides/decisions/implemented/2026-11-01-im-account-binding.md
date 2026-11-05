# IM 账号绑定

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/im_binding_service.py

## 问题

IM 机器人用「机器人归属人」的 API Key 执行所有消息：同一机器人下任何飞书/钉钉用户都以归属人身份运行，线程、知识库、附件和历史全部共享。`allowed_users` 只能拦人，不能表达「这条消息属于哪个 Yuxi 用户」。员工与 Yuxi 账号是一对一关系，需要把飞书/钉钉身份稳定映射到自己的 Yuxi 账号，使 IM 消息按本人权限与线程执行。

## 决策

复用机器人凭据做平台 OAuth：绑定入口在 Yuxi 个人设置内，用该平台唯一启用的机器人凭据发起飞书/钉钉扫码授权，回调换取平台用户 ID 写入映射表。零新增配置、多机器人天然隔离；代价是所属应用要配好回调地址与授权范围，换应用后 open_id 变化需重新绑定。

### 实现方案

**持久化**（随 `storage_migrations` 落地）：

- `im_account_bindings(id, user_id, platform, platform_user_id, bot_id, created_at, updated_at)`，`unique(platform, platform_user_id)` 与 `unique(user_id, platform)`：一个 Yuxi 用户在同一平台只保留一条绑定，一个平台身份不能被两个用户占用。
- `im_binding_sessions(id, user_id, platform, bot_id, state_hash, status, expires_at, used_at)`：绑定二维码背后的一次性 state，只存 sha256 哈希，10 分钟有效期，用后置 `used_at`，重放必须失败。
- `im_bot_configs.require_binding`（布尔，默认 `false`）：过渡期开关。关闭时未绑定身份回退到机器人归属人并附提示，打开后未绑定身份直接拦截。

**绑定链路**（入口在 Yuxi 个人设置，`web/src/components/ImAccountBindingComponent.vue` 负责前端交互）：

1. `POST /api/user/im-bindings/{platform}/sessions`：取该平台唯一启用的机器人凭据生成 state，返回 `{bind_url, expires_at}`；前端用 `bind_url` 渲染二维码（`qrcode` 前端库，后端不存图片）。同一平台多个启用机器人时报错要求收敛，不静默挑选凭据。
2. `GET /api/im-bindings/callback/{platform}?code=&state=`（公开路由，靠 state 鉴权）：校验 state 未过期未使用 → 换 user access token → 取平台身份（飞书 `open_id`；钉钉 `unionId` 经 `topapi/v2/user/getbyunionid` 换成消息侧 `userid`）→ 写绑定，返回一页最小 HTML 结果页。
3. `GET /api/user/im-bindings` 列出本人绑定；`DELETE /api/user/im-bindings/{platform}` 解绑。

**消息链路**：`backend/server/routers/agent_invocation_channel_router.py` 保留「API Key = 机器人归属人」的传输身份，仅当 `channel` 属于 IM 平台（`feishu` / `dingtalk`）时校验调用方确实是该 `account_id` 机器人（`bot.owner_uid == current_user.uid` 否则 403，机器人停用返回 403 `im_bot_disabled`），随后 `resolve_channel_identity` 按 `(bot.platform, sender_id)` 查绑定得到真实用户，以该用户身份提交 Run（线程、权限、模型配置、附件全按该用户走），并在成功响应返回 `identity_source`。同一解析对 `/resume` 与 slash command 生效，避免两个入口身份不一致；非 IM 渠道（如 `yuxi-cli`）保持原有透传身份与响应结构，不进入绑定解析。未绑定时按机器人 `require_binding` 分流：打开返回 `409 im_account_not_bound`，网关（`packages/yuxi-im-gateway`）回一条「请先在 Yuxi 个人设置里绑定飞书/钉钉账号」；关闭（过渡期）仍以机器人归属人身份执行，响应带 `identity_source=owner_fallback`，网关在正文后附一行未绑定提示。

绑定后 Run 的 `uid` 是绑定用户，而网关仍只持有机器人归属人的 API Key。运行事件流与线程产物下载在原有「Run / 线程属于调用方」校验之外增加一条 Channel 读权限：Run 来源为 `channel`，其 `origin_metadata.account_id` 解析到仍启用且归属调用方的机器人时允许读取；非 Channel Run、机器人缺失、停用或归属他人一律不解锁。该规则集中实现在 `im_binding_service.can_read_channel_run_as_bot_owner` 与 `can_read_channel_thread_as_bot_owner`，分别供 `agent_run_service` 事件流与 `artifact_service` 产物下载复用。

Owner 分工：`im_binding_service.py` 拥有绑定会话、平台身份换取与绑定/身份解析；`agent_invocation_channel_router.py` 拥有消息入口的身份解析接入与传输身份校验；`ImAccountBindingComponent.vue` 拥有个人设置页入口与二维码交互；`packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py` 拥有 `owner_fallback` 与 `im_account_not_bound` 的提示文案。

## 替代方案

- **平台级 OAuth 配置**：新增独立于机器人的飞书/钉钉授权应用配置，绑定与机器人解耦；代价是同一套密钥在两处维护，后台表单与加密存储再做一份。
- **把飞书/钉钉做成 SSO 登录方式**：扫码即登录并顺带建立映射；代价是登录链路、会话与用户开通策略同时改动，绑定语义与登录语义混在一起，风险最高。
- **复用机器人凭据做平台 OAuth（已选）**：现有 `im_bot_configs` 已按平台加密保存 `app_id/app_secret`（飞书）与 `client_id/client_secret`（钉钉），且已有 OAuth 回调式流程（`/auth/oidc/*` 的 state + 换 code + 前端回调页）可作实现模板；当前每个平台只有一个机器人在跑，代价最低。

## 后果

- 过渡期硬切换：`require_binding` 默认关闭，先上线绑定能力与归属人回退提示；使用该机器人的同事完成绑定后，由管理员逐个机器人打开开关，硬切后未绑定身份被拦截。
- 线程不连续：绑定改变 uid 后 `channel_*` 线程 ID 随之变化，绑定前的 IM 会话历史不续接；如需迁移旧会话须单独立项。
- 一个平台同时启用多个机器人时绑定会话直接报错，要求管理员收敛。
- 钉钉 `unionId → userid` 依赖通讯录权限，换取失败即整次绑定失败并回明确提示，不做降级匹配。
- 回调地址取自创建会话时的请求 `base_url`，要求该域名手机侧可达且已在平台后台登记。
- `allowed_users` 白名单继续作为入口门禁，与绑定相互独立。
- Channel Run 的读权限跟随机器人启停：机器人停用后，归属人不再能通过该规则读取其历史 Channel Run 事件与产物。

## 验证

本槽位真实执行的命令与结果：

- `docker compose exec -T api python -m pytest test/unit -m "not slow" -q` → **2680 passed, 58 skipped**。
- `docker compose exec -T api python -m pytest test/unit/routers/test_agent_invocation_channel_router.py test/unit/services/test_artifact_service.py -q` → **34 passed**（含非 IM 渠道不解析绑定、产物回退到 Channel Run 归属人与拒绝无归属两条负向用例）。
- `docker compose exec -T api python -m pytest test/integration/services/test_channel_run_read_access.py -q` → **2 passed**（真实 PostgreSQL：绑定用户的 Channel Run 由机器人归属人可读、无关用户不可读、普通 Chat Run 不解锁、停用机器人不解锁）。
- 负向验证：临时移除 `channel in PLATFORMS` 门槛 → `test_cli_channel_skips_im_binding_resolution` 变红；临时让 `can_read_channel_run_as_bot_owner` 恒为 `False` → `test_channel_run_is_readable_by_bot_owner_and_thread_artifact_resolves` 变红，恢复后均通过。
- `docker compose exec -T api python -m pytest test/unit/services/test_im_binding_service.py -q` → **3 passed**。
- `docker compose exec -T api python -m pytest test/integration/services/test_im_binding_service.py test/integration/storage/test_im_binding_schema.py test/integration/api/test_im_binding_router.py test/integration/api/test_channel_identity_binding.py -q` → **23 passed, 2 skipped**（真实 PostgreSQL，含绑定命中、归属人回退、强绑拦截、机器人归属与停用校验、唯一约束与 state 状态机）。
- `cd packages/yuxi-im-gateway && uv run --group test pytest tests -q` → **66 passed**。
- `cd web && pnpm test:unit` → **472 passed**；`pnpm lint:check` → **exit 0**；`pnpm build` → **构建成功**（本槽位宿主机 pnpm 版本管理器损坏、Docker web 镜像 node_modules 过期，改为直接调用本地 pnpm 10.19.0 与宿主 node_modules 构建通过，见「未验证」）。
- `python3 -m unittest scripts.test_verify_engineering_contracts` → **63 tests OK**。
- `python3 scripts/verify_engineering_contracts.py` → 移入 `implemented/` 前因 `proposed/` 记录格式不符报 5 个问题；本次移动并改写后恢复通过。

未验证（本槽位无凭据或依赖外部环境）：

- **真实飞书/钉钉扫码链路**：需有测试应用的飞书/钉钉租户凭据，本槽位未配置，未验证。
- **E2E 用例 `backend/test/e2e/test_im_binding_flow.py`**：真实 Channel 入口 + 真实 PostgreSQL，验证两条不同 `sender_id` 分别落库到两个 uid 的 `channel_*` 线程、`agent_runs.uid` 与会话归属一致、未绑定身份在 `require_binding` 打开时被拦。本槽位为 SaaS 登录环境且未配置 `E2E_USERNAME`/`E2E_PASSWORD`，运行时 `1 skipped`，未验证真实 HTTP 主链路。
- **基于 HTTP 凭据的集成用例**（如 `test_im_binding_router.py` 的 2 条、`test_agent_invocation_channel_api.py`）：本槽位未配置管理员凭据，跳过。
- **Docker web 镜像内 `pnpm build`**：镜像 node_modules 早于 `qrcode` 依赖提交，容器内构建报 `Rolldown failed to resolve import "qrcode"`；宿主 `web/package.json` 与 `web/pnpm-lock.yaml` 均含 `qrcode`，非代码缺陷，属镜像过期。
