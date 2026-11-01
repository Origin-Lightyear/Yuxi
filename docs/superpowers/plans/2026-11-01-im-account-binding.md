# IM 账号绑定（飞书 / 钉钉）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 员工在 Yuxi 个人设置里生成绑定链接/二维码，用飞书或钉钉扫码授权后，把平台身份与自己的 Yuxi 账号一一绑定；此后 IM 消息按本人身份执行，未绑定期间按机器人归属人执行并明确提示。

**Architecture:** 新增 `im_account_bindings`（平台身份 ↔ 用户映射）与 `im_binding_sessions`（一次性 state）两张表，加上 `im_bot_configs.require_binding` 开关。绑定入口在 Yuxi 侧，用该平台唯一启用机器人的凭据发起平台 OAuth，回调换取平台用户 ID 后落库。Channel 入口保留「API Key = 机器人归属人」的传输身份用于校验调用方，再按 `(platform, sender_id)` 解析真实用户；未绑定时按开关分流为「回退归属人 + 提示」或「409 拦截」。

**Tech Stack:** FastAPI + SQLAlchemy(async) + PostgreSQL、httpx、Vue 3 + Ant Design Vue、`qrcode`（前端渲染二维码）、pytest / vitest。

**Spec:** `docs/develop-guides/decisions/proposed/2026-11-01-im-account-binding.md`

## Global Constraints

- Python `>=3.12`；ruff `line-length = 120`，规则 `F,E,W,UP`；新增函数/类写中文 docstring。
- 新增表/列必须同时进 `models_business.py` 与 `manager.ensure_business_schema()`（列）或 `create_business_tables`（表），API 与 worker 启动时都会跑。
- 凭证只从 `im_bot_service.decrypt_credentials()` 取，任何日志、响应、测试输出不得出现明文 secret。
- state 只存哈希，有效期 600 秒，用后置 `used_at`，重放必须失败。
- 绑定查询与身份回落必须显式：未绑定且开关打开时返回 `409`，不静默使用归属人身份。
- 每个新增 guard 配一个负向测试（恢复缺陷即变红）。
- 提交前在仓库根目录执行：`python3 scripts/verify_engineering_contracts.py`、`python3 -m unittest scripts.test_verify_engineering_contracts`、`docker compose exec api uv run --group test pytest test/unit -m "not slow"`；网关改动额外跑 `cd packages/yuxi-im-gateway && uv run --group test pytest tests -q`；前端跑 `cd web && pnpm lint:check && pnpm test:unit`。

## Review Focus

- **未绑定身份是否能绕过开关执行**：`require_binding=true` 时任何 `sender_id` 缺失、未命中绑定的请求都必须 409，不能落到归属人身份。
- **伪造 `account_id`**：持有任意 API Key 的调用方不能通过传别人的 `account_id` 拿到别人的绑定身份，必须校验机器人归属。
- **同平台身份被换绑**：另一个用户完成授权时不能覆盖已有绑定，必须拒绝并引导先解绑。
- **state 重放 / 过期**：用过或过期的 state 必须失败，不能写出绑定。
- **过渡期静默**：`require_binding=false` 且未绑定时，响应必须带 `identity_source=owner_fallback`，网关必须附提示，用户不能毫不知情。

---

### Task 1: 持久化模型与 schema 演进

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`（`IMBotConfig` 之后新增两个模型，`IMBotConfig` 增加 `require_binding` 列与序列化字段）
- Modify: `backend/package/yuxi/storage/postgres/manager.py:963` 起的 `ensure_business_schema`
- Modify: `backend/package/yuxi/services/im_bot_service.py`（`to_dict`/创建/更新入参带 `require_binding`）
- Test: `backend/test/integration/storage/test_im_binding_schema.py`

**Interfaces:**
- Produces: `IMAccountBinding(user_id, platform, platform_user_id, bot_id, created_at, updated_at)`，`IMBindingSession(user_id, platform, bot_id, state_hash, status, expires_at, used_at, created_at)`，`IMBotConfig.require_binding: bool`。

- [ ] **Step 1: 写失败测试**

```python
# backend/test/integration/storage/test_im_binding_schema.py
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from yuxi.storage.postgres.models_business import IMAccountBinding, IMBotConfig


async def test_binding_unique_constraints(db_session):
    db_session.add_all([
        IMAccountBinding(user_id=1, platform="dingtalk", platform_user_id="u1", bot_id=None),
        IMAccountBinding(user_id=2, platform="dingtalk", platform_user_id="u1", bot_id=None),
    ])
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

再补一条断言同一用户同平台第二条绑定同样 `IntegrityError`，以及 `IMBotConfig.require_binding` 默认 `False`。

- [ ] **Step 2: 运行确认失败**

`docker compose exec api uv run --group test pytest test/integration/storage/test_im_binding_schema.py -q`
期望：`ImportError: cannot import name 'IMAccountBinding'`。

- [ ] **Step 3: 加模型与 schema 语句**

`IMAccountBinding`：`user_id` 外键 `users.id`（`ondelete="CASCADE"`）、`platform String(16)`、`platform_user_id String(128)`、`bot_id Integer FK im_bot_configs.id SET NULL`、时间戳；`__table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_im_binding_platform_user"), UniqueConstraint("user_id", "platform", name="uq_im_binding_user_platform"))`。
`IMBindingSession`：`state_hash String(64) unique`、`status String(16) default "pending"`、`expires_at`、`used_at`。
`ensure_business_schema` 增加一行：`ALTER TABLE IF EXISTS im_bot_configs ADD COLUMN IF NOT EXISTS require_binding BOOLEAN NOT NULL DEFAULT false`。

- [ ] **Step 4: 跑测试通过**

同 Step 2 命令；期望 `passed`。

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/package/yuxi/services/im_bot_service.py backend/test/integration/storage/test_im_binding_schema.py
git commit -m "feat(im-binding): 新增 IM 账号绑定与绑定会话数据模型"
```

---

### Task 2: 绑定会话、平台授权与身份解析服务

**Files:**
- Create: `backend/package/yuxi/services/im_binding_service.py`
- Test: `backend/test/unit/services/test_im_binding_service.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ChannelIdentity: user: User; owner_fallback: bool`
  - `async def create_binding_session(db, *, user: User, platform: str, base_url: str) -> dict`（返回 `{"bind_url", "expires_at"}`）
  - `async def complete_binding(db, *, platform: str, code: str, state: str) -> str`（返回前端回调路径）
  - `async def list_bindings(db, *, user_id: int) -> list[dict]`
  - `async def delete_binding(db, *, user: User, platform: str) -> bool`
  - `async def resolve_channel_identity(db, *, transport_user: User, account_id: str, sender_id: str | None) -> ChannelIdentity`（未绑定且 `require_binding` 时抛 `HTTPException(409, detail={"code": "im_account_not_bound", ...})`；`account_id` 不归属 `transport_user` 时抛 403）
  - 内部：`_build_authorize_url(platform, credentials, redirect_uri, state) -> str`、`_exchange_code(platform, credentials, code, redirect_uri) -> tuple[str, str | None]`（返回 `(platform_user_id, union_id)`）、`_dingtalk_userid(credentials, union_id) -> str`

- [ ] **Step 1: 写失败测试**（`backend/test/unit/services/test_im_binding_service.py`）

覆盖五个用例，全部用内存 SQLite/现有 `db_session` fixture 与 monkeypatch 的平台函数：
1. `test_create_binding_session_rejects_ambiguous_platform`：两个启用钉钉机器人时抛 `ValueError("同一平台只能有一个启用的机器人")`。
2. `test_complete_binding_consumes_state_once`：第一次成功写入绑定；同一 state 第二次调用抛 `HTTPException(400, detail={"code": "invalid_binding_state", ...})`。
3. `test_complete_binding_rejects_identity_owned_by_other_user`：已有他人绑定时抛 `HTTPException(409, detail={"code": "im_account_already_bound", ...})`。
4. `test_complete_binding_rejects_expired_state`：`expires_at` 已过期时抛 `HTTPException(400, detail={"code": "invalid_binding_state", ...})`，且不写绑定。
5. `test_resolve_channel_identity_falls_back_to_owner_when_not_required`：`require_binding=False` 且无绑定时返回 `ChannelIdentity(user=transport_user, owner_fallback=True)`；`require_binding=True` 时抛 409 `im_account_not_bound`。

- [ ] **Step 2: 运行确认失败**

`docker compose exec api uv run --group test pytest test/unit/services/test_im_binding_service.py -q` → `ModuleNotFoundError`。

- [ ] **Step 3: 实现服务**

授权 URL：飞书 `https://open.feishu.cn/open-apis/authen/v1/index`（`app_id`、`redirect_uri`、`state`）；钉钉 `https://login.dingtalk.com/oauth2/auth`（`response_type=code`、`client_id`、`redirect_uri`、`scope=openid`、`state`、`prompt=consent`）。
换取身份：飞书 `POST /open-apis/authen/v1/access_token`（`tenant_access_token` 换 `user_access_token`）后读 `data.open_id`/`data.union_id`；钉钉 `POST https://api.dingtalk.com/v1.0/oauth2/userAccessToken` 拿 `accessToken`，再 `GET https://api.dingtalk.com/v1.0/contact/users/me` 取 `unionId`，最后 `POST https://oapi.dingtalk.com/topapi/v2/user/getbyunionid` 取 `result.userid`。
`resolve_channel_identity`：先按 `bot_id == int(account_id)` 取机器人并校验 `bot.owner_uid == transport_user.uid`，再按 `(bot.platform, sender_id)` 查绑定，命中则查出绑定用户返回 `owner_fallback=False`。

- [ ] **Step 4: 跑测试通过**（同 Step 2 命令）

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/services/im_binding_service.py backend/test/unit/services/test_im_binding_service.py
git commit -m "feat(im-binding): 实现绑定会话与平台授权换绑"
```

---

### Task 3: 用户侧绑定接口与公开回调路由

**Files:**
- Create: `backend/server/routers/im_binding_router.py`
- Modify: `backend/server/routers/__init__.py`
- Test: `backend/test/integration/api/test_im_binding_router.py`

**Interfaces:**
- Produces: `GET /api/user/im-bindings`、`POST /api/user/im-bindings/{platform}/sessions`、`DELETE /api/user/im-bindings/{platform}`、`GET /api/im-bindings/callback/{platform}`（公开，成功/失败都返回一页最小 HTML 结果页，供手机端扫码后直接看，不新增前端路由）。

- [ ] **Step 1: 写失败测试（真实 HTTP）**

四个用例：未登录访问 `/api/user/im-bindings` 返回 401；`POST .../sessions` 返回 `bind_url` 且 URL 含 `state=`；带错误 state 的回调返回 200 且页面含「绑定失败」；`DELETE` 后列表为空。

- [ ] **Step 2: 运行确认失败**（`... pytest backend/test/integration/api/test_im_binding_router.py -q`，期望 404）

- [ ] **Step 3: 实现路由**

路由保持薄：只做参数校验与 `im_binding_service` 调用；`redirect_uri` 用 `str(request.base_url).rstrip("/") + "/api/im-bindings/callback/{platform}"`；回调用 `HTMLResponse` 返回「绑定成功，可关闭此页」/「绑定失败：<原因>」，不抛未处理异常。

- [ ] **Step 4: 跑测试通过**

- [ ] **Step 5: 提交**

```bash
git add backend/server/routers/im_binding_router.py backend/server/routers/__init__.py backend/test/integration/api/test_im_binding_router.py
git commit -m "feat(im-binding): 提供绑定会话、回调与解绑接口"
```

---

### Task 4: Channel 入口按绑定解析身份

**Files:**
- Modify: `backend/server/routers/agent_invocation_channel_router.py`
- Test: `backend/test/integration/api/test_channel_identity_binding.py`

**Interfaces:**
- Consumes: `resolve_channel_identity`、`ChannelIdentity`（Task 2）。
- Produces: Channel 响应体新增 `identity_source: "binding" | "owner_fallback"`。

- [ ] **Step 1: 写失败测试**

1. 已绑定 sender 的消息：run 的 `uid` 等于绑定用户 uid，响应 `identity_source == "binding"`。
2. 未绑定 + `require_binding=false`：run 的 `uid` 等于机器人归属人，响应 `identity_source == "owner_fallback"`。
3. 未绑定 + `require_binding=true`：HTTP 409，`detail.code == "im_account_not_bound"`，且不产生 run。
4. 用 A 的 API Key 提交 `account_id=B` 的机器人：403。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

在 `receive_channel_message` 与 `_resolve_latest_run` 之前调用一次 `identity = await resolve_channel_identity(...)`，后续 `current_user` 全部改用 `identity.user`（线程 id、request_id 哈希、`submit_agent_request`），响应里加 `identity_source`。slash command 与 `/resume` 走同一解析。

- [ ] **Step 4: 跑测试通过**（含既有 `test/unit/routers/test_agent_invocation_channel_decisions.py`）

- [ ] **Step 5: 提交**

```bash
git add backend/server/routers/agent_invocation_channel_router.py backend/test/integration/api/test_channel_identity_binding.py
git commit -m "feat(im-binding): Channel 入口按绑定身份解析发送者"
```

---

### Task 5: 网关识别未绑定与过渡期提示

**Files:**
- Modify: `packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py`
- Test: `packages/yuxi-im-gateway/tests/test_runner.py`

**Interfaces:**
- Consumes: Channel 响应 `identity_source`、`409 im_account_not_bound`。

- [ ] **Step 1: 写失败测试**

`submit_message` 返回 `{"thread_id": "t1", "identity_source": "owner_fallback", ...}` 且带一条 markdown 事件时，`adapter.markdowns[0][1]` 末尾含「你还没绑定 Yuxi 账号」；`submit_message` 抛 `httpx.HTTPStatusError(409, detail={"code": "im_account_not_bound"})` 时，`adapter.texts == [("c1", "请先在 Yuxi 个人设置里完成飞书/钉钉账号绑定后再试。")]`。

- [ ] **Step 2: 运行确认失败**（`cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_runner.py -q`）

- [ ] **Step 3: 实现**

`_submit_error_text` 增加 `im_account_not_bound` 分支；`_deliver` 增加 `suffix: str | None = None` 参数并把提示追加到 markdown 正文末尾；`_run_and_reply` 按 `submitted.get("identity_source")` 传 suffix。

- [ ] **Step 4: 跑测试通过**

- [ ] **Step 5: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py packages/yuxi-im-gateway/tests/test_runner.py
git commit -m "feat(im-binding): 网关提示未绑定与过渡期身份回退"
```

---

### Task 6: 个人设置「IM 账号」绑定页

**Files:**
- Create: `web/src/apis/im_binding_api.js`
- Create: `web/src/components/ImAccountBindingComponent.vue`
- Modify: `web/src/components/SettingsModal.vue`（新增 `imAccounts` tab，登录用户可见，标题「IM 账号」）
- Modify: `web/src/components/ImBotManagementComponent.vue`（机器人表单新增「要求绑定后才能使用」开关，提交字段 `require_binding`）
- Modify: `web/package.json`（新增 `qrcode` 依赖）
- Test: `web/src/components/__tests__/ImAccountBindingComponent.spec.js`

**Interfaces:**
- Consumes: Task 3 的四个接口。

- [ ] **Step 1: 写失败测试**（vitest + `@vue/test-utils`）

断言：无绑定时渲染「绑定飞书 / 绑定钉钉」按钮；点击后调用 `createSession` 并把返回的 `bind_url` 传给二维码组件；已绑定时显示可解绑状态。另断言 `ImBotManagementComponent` 提交的 payload 含 `require_binding`。

- [ ] **Step 2: 运行确认失败**（`cd web && pnpm test:unit -- ImAccountBindingComponent`）

- [ ] **Step 3: 实现 API 模块与组件**

`im_binding_api.js` 用 `apiGet/apiPost/apiDelete`（普通用户接口，非 admin）；组件内二维码用 `qrcode` 的 `toDataURL` 渲染 `<img>`；弹窗打开时每 3 秒轮询 `list()`，命中绑定即关闭并刷新状态。

- [ ] **Step 4: 跑测试与 lint**（`pnpm test:unit -- ImAccountBindingComponent && pnpm lint:check`）

- [ ] **Step 5: 提交**

```bash
git add web/src/apis/im_binding_api.js web/src/components/ImAccountBindingComponent.vue web/src/components/SettingsModal.vue web/src/components/ImBotManagementComponent.vue web/src/components/__tests__/ImAccountBindingComponent.spec.js web/package.json web/pnpm-lock.yaml
git commit -m "feat(im-binding): 个人设置支持扫码绑定飞书/钉钉账号"
```

---

### Task 7: 接线、证据与文档收尾

**Files:**
- Modify: `docs/develop-guides/changelog.md`
- Modify: `docs/develop-guides/decisions/proposed/2026-11-01-im-account-binding.md`（按实际实现改写，移入 `implemented/`）
- Test: `backend/test/e2e/test_im_binding_flow.py`（真实 Channel 入口 + 真实 PostgreSQL）

- [ ] **Step 1: 写端到端用例**

两条不同 `sender_id`（均已绑定不同用户）的消息分别落库到两个 uid 的 `channel_*` 线程，并校验 `agent_runs.uid` 与会话归属一致；未绑定身份在开关打开时被拦。

- [ ] **Step 2: 运行确认失败**

`docker compose exec api uv run --group test pytest test/e2e/test_im_binding_flow.py -q`

- [ ] **Step 3: 跑完整校验并记录实际结果**

依次执行 Global Constraints 里的四条命令 + 网关测试 + 前端 lint/unit，把「实际命令 / 结果 / 未验证项（真实飞书/钉钉扫码需测试应用凭据）」写进 decision record 的 `## 验证`。

- [ ] **Step 4: 收尾文档**

changelog 增一条（≤200 字）；decision record 移入 `docs/develop-guides/decisions/implemented/` 并改写为现在时。

- [ ] **Step 5: 提交**

```bash
git add docs/develop-guides/changelog.md docs/develop-guides/decisions backend/test/e2e/test_im_binding_flow.py
git commit -m "docs(im-binding): 记录 IM 账号绑定的证据与未验证范围"
```
