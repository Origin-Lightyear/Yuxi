# IM 账号绑定（飞书 / 钉钉）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 员工在 Yuxi 个人设置里生成绑定链接/二维码，用飞书或钉钉扫码授权后，把平台身份与自己的 Yuxi 账号一一绑定；此后 IM 消息按本人身份执行，未绑定期间按机器人归属人执行并明确提示。

**Architecture:** 新增 `im_account_bindings`（平台身份 ↔ 用户映射）与 `im_binding_sessions`（一次性 state）两张表，加上 `im_bot_configs.require_binding` 开关。绑定入口在 Yuxi 侧，用该平台唯一启用机器人的凭据发起平台 OAuth，回调换取平台用户 ID 后落库。Channel 入口保留「API Key = 机器人归属人」的传输身份用于校验调用方，再按 `(platform, sender_id)` 解析真实用户；未绑定时按开关分流为「回退归属人 + 提示」或「409 拦截」。

> **修订（Phase 2）**：下方 Phase 1 的 `im_account_bindings` 本地表设计已被取代。最终方案不再由 Yuxi 保存绑定关系，Tenant 是唯一保存方，Yuxi 只保留 `im_binding_sessions` 一次性 state 与 TTL 对齐的 Redis 缓存。以本计划末尾 Phase 2 任务与决策记录为准。

**Tech Stack:** FastAPI + SQLAlchemy(async) + PostgreSQL、httpx、Vue 3 + Ant Design Vue、`qrcode`（前端渲染二维码）、pytest / vitest。

**Spec:** `docs/develop-guides/decisions/implemented/2026-11-01-im-account-binding.md`

## Global Constraints

- Python `>=3.12`；ruff `line-length = 120`，规则 `F,E,W,UP`；新增函数/类写中文 docstring。
- 新增表/列必须同时进 `models_business.py` 与 `manager.ensure_business_schema()`（列）或 `create_business_tables`（表），API 与 worker 启动时都会跑。
- 凭证只从 `im_bot_service.decrypt_credentials()` 取，任何日志、响应、测试输出不得出现明文 secret。
- state 只存哈希，有效期 600 秒，用后置 `used_at`，重放必须失败。
- 绑定查询与身份回落必须显式：未绑定且开关打开时返回 `409`，不静默使用归属人身份。
- 每个新增 guard 配一个负向测试（恢复缺陷即变红）。
- 提交前在仓库根目录执行：`python3 scripts/verify_engineering_contracts.py`、`python3 -m unittest scripts.test_verify_engineering_contracts`、`docker compose exec api uv run --group test pytest test/unit -m "not slow"`；网关改动额外跑 `cd packages/yuxi-im-gateway && uv run --group test pytest tests -q`；前端跑 `cd web && pnpm lint:check && pnpm test:unit`。
- 集成测试一律在 api 容器里跑，但本 worktree 的镜像里 `uv run` 会因 site-packages 权限失败，改用 `docker compose exec -T api python -m pytest test/integration/... -q`；ruff 用宿主机 `uv run --with ruff ruff check --line-length 120 --select F,E,W,UP <files>`。
- 原「```docker compose exec api uv run --group test pytest```」命令在本槽位不可用，同理替换为 `docker compose exec -T api python -m pytest test/unit -m "not slow"`。，真实 PostgreSQL 通过 `os.environ["POSTGRES_URL"]` 自建 engine，按 `test_agent_run_lease.py` 的做法建表、用 `uuid4` 造唯一数据并在 `finally` 里删除，不依赖缺失的 `db_session` fixture。
- 仓库没有 `backend/test/.env.test`（只有 `.example`）；HTTP 集成测试需要它提供 `TEST_USERNAME`/`TEST_PASSWORD`。本地按本 worktree 的 `.env` 生成该文件（git 忽略，不提交），缺失时相关用例会 skip，不能算通过。

## Review Focus

- **未绑定身份是否能绕过开关执行**：`require_binding=true` 时任何 `sender_id` 缺失、未命中绑定的请求都必须 409，不能落到归属人身份。
- **伪造 `account_id`**：持有任意 API Key 的调用方不能通过传别人的 `account_id` 拿到别人的绑定身份，必须校验机器人归属。
- **同平台身份被换绑**：另一个用户完成授权时不能覆盖已有绑定，必须拒绝并引导先解绑。
- **state 重放 / 过期**：用过或过期的 state 必须失败，不能写出绑定。
- **过渡期静默**：`require_binding=false` 且未绑定时，响应必须带 `identity_source=owner_fallback`，网关必须附提示，用户不能毫不知情；非 SaaS 部署直接按归属人执行，返回 `identity_source=owner`，不附绑定提示。

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
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.models_business import IMAccountBinding, IMBotConfig

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def binding_engine():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text(
            "ALTER TABLE IF EXISTS im_bot_configs ADD COLUMN IF NOT EXISTS require_binding BOOLEAN NOT NULL DEFAULT false"
        ))
        await connection.run_sync(IMAccountBinding.__table__.create, checkfirst=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_binding_unique_constraints(binding_engine):
    uid = uuid.uuid4().hex[:8]
    async with binding_engine() as session:
        session.add(IMAccountBinding(user_id=1, platform="dingtalk", platform_user_id=f"u-{uid}"))
        session.add(IMAccountBinding(user_id=2, platform="dingtalk", platform_user_id=f"u-{uid}"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
```

再补两条：同一用户同平台第二条绑定同样 `IntegrityError`；`IMBotConfig.require_binding` 的 `default.arg` 为 `False`（纯 metadata 断言，无需连库）。

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
- Test: `backend/test/unit/services/test_im_binding_service.py`（纯逻辑）、`backend/test/integration/services/test_im_binding_service.py`（真实 PostgreSQL）

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ChannelIdentity: user: User; identity_source: str`（`binding` / `owner_fallback` / `owner`）
  - `async def create_binding_session(db, *, user: User, platform: str, base_url: str) -> dict`（返回 `{"bind_url", "expires_at"}`）
  - `async def complete_binding(db, *, platform: str, code: str, state: str) -> str`（返回前端回调路径）
  - `async def list_bindings(db, *, user_id: int) -> list[dict]`
  - `async def delete_binding(db, *, user: User, platform: str) -> bool`
  - `async def resolve_channel_identity(db, *, transport_user: User, account_id: str, sender_id: str | None) -> ChannelIdentity`（未绑定且 `require_binding` 时抛 `HTTPException(409, detail={"code": "im_account_not_bound", ...})`；`account_id` 不归属 `transport_user` 时抛 403）
  - 内部：`_build_authorize_url(platform, credentials, redirect_uri, state) -> str`、`_exchange_code(platform, credentials, code, redirect_uri) -> tuple[str, str | None]`（返回 `(platform_user_id, union_id)`）、`_dingtalk_userid(credentials, union_id) -> str`

- [ ] **Step 1: 写失败测试**

`backend/test/unit/services/test_im_binding_service.py`（不连库，只测纯函数）：`_build_authorize_url("dingtalk", credentials, redirect_uri, state)` 含 `client_id=`、`state=`、`scope=openid`；`_build_authorize_url("feishu", ...)` 指向 `open-apis/authen/v1/index`；`_hash_state("abc")` 稳定且不等于明文。

`backend/test/integration/services/test_im_binding_service.py`（真实 PostgreSQL，按 Task 1 的 engine fixture 建表，monkeypatch 平台换取函数）覆盖五个用例：
1. `test_create_binding_session_rejects_ambiguous_platform`：两个启用钉钉机器人时抛 `ValueError("同一平台只能有一个启用的机器人")`。
2. `test_complete_binding_consumes_state_once`：第一次成功写入绑定；同一 state 第二次调用抛 `HTTPException(400, detail={"code": "invalid_binding_state", ...})`。
3. `test_complete_binding_rejects_identity_owned_by_other_user`：已有他人绑定时抛 `HTTPException(409, detail={"code": "im_account_already_bound", ...})`。
4. `test_complete_binding_rejects_expired_state`：`expires_at` 已过期时抛 `HTTPException(400, detail={"code": "invalid_binding_state", ...})`，且不写绑定。
5. `test_resolve_channel_identity_falls_back_to_owner_when_not_required`：`require_binding=False` 且无绑定时返回 `identity_source="owner_fallback"`；`require_binding=True` 时抛 409 `im_account_not_bound`；非 SaaS 返回 `identity_source="owner"`。

- [ ] **Step 2: 运行确认失败**

`docker compose exec api uv run --group test pytest test/unit/services/test_im_binding_service.py test/integration/services/test_im_binding_service.py -q` → `ModuleNotFoundError`。

- [ ] **Step 3: 实现服务**

授权 URL：飞书 `https://open.feishu.cn/open-apis/authen/v1/index`（`app_id`、`redirect_uri`、`state`）；钉钉 `https://login.dingtalk.com/oauth2/auth`（`response_type=code`、`client_id`、`redirect_uri`、`scope=openid`、`state`、`prompt=consent`）。
换取身份：飞书 `POST /open-apis/authen/v1/access_token`（`tenant_access_token` 换 `user_access_token`）后读 `data.open_id`/`data.union_id`；钉钉 `POST https://api.dingtalk.com/v1.0/oauth2/userAccessToken` 拿 `accessToken`，再 `GET https://api.dingtalk.com/v1.0/contact/users/me` 取 `unionId`，最后 `POST https://oapi.dingtalk.com/topapi/v2/user/getbyunionid` 取 `result.userid`。
`resolve_channel_identity`：先按 `bot_id == int(account_id)` 取机器人并校验 `bot.owner_uid == transport_user.uid`，再按 `(bot.platform, sender_id)` 查绑定，命中则查出绑定用户返回 `identity_source="binding"`。

- [ ] **Step 4: 跑测试通过**（同 Step 2 命令）

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/services/im_binding_service.py backend/test/unit/services/test_im_binding_service.py backend/test/integration/services/test_im_binding_service.py
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

- [ ] **Step 0: 准备本地集成测试凭据**

`cp backend/test/.env.test.example backend/test/.env.test`，把 `TEST_USERNAME`/`TEST_PASSWORD` 填成本 worktree `.env` 里的超级管理员账号（git 忽略，不提交）。

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
- Produces: Channel 响应体新增 `identity_source: "binding" | "owner_fallback" | "owner"`。

- [ ] **Step 1: 写失败测试**

1. 已绑定 sender 的消息：run 的 `uid` 等于绑定用户 uid，响应 `identity_source == "binding"`。
2. 未绑定 + `require_binding=false`（SaaS）：run 的 `uid` 等于机器人归属人，响应 `identity_source == "owner_fallback"`；非 SaaS 返回 `identity_source == "owner"`。
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

---

## Phase 2: Tenant 是绑定关系唯一保存方

**背景：** Phase 1 把绑定关系写入 Yuxi 的 `im_account_bindings`，与 Tenant 的 `employee/channel-bindings` 形成两份事实。Phase 2 改为 Tenant 唯一保存，Yuxi 只保留一次性 OAuth state（`im_binding_sessions`）和一份 TTL 与 Tenant AgentSession 对齐的 Redis 映射缓存，用于 IM 消息侧避免每条消息都调用 Tenant。非 SaaS 部署不启用绑定，IM 继续按机器人归属人执行。

**Tenant 契约：** `docs/advanced/agent-channel-binding-api.md`（四接口、`AgentSession` 员工认证、`X-Tenant-Admin-Key` 平台登录、错误码 `20314/20316/20302/20317/20206`）。

### Task 8: Tenant 渠道绑定客户端与 Redis 缓存原语

**Files:**
- Modify: `backend/package/yuxi/services/saas_client.py`
- Create: `backend/package/yuxi/services/im_binding_cache.py`
- Test: `backend/test/unit/services/test_saas_client.py`、`backend/test/unit/services/test_im_binding_cache.py`

**Interfaces:**
- Produces:
  - `SaasClient.bind_channel_account(agent_session_token: str, channel_type: str, channel_user_id: str) -> dict`
  - `SaasClient.unbind_channel_account(agent_session_token: str, channel_type: str) -> None`
  - `SaasClient.list_channel_bindings(agent_session_token: str) -> list[dict]`
  - `SaasClient.authenticate_channel(channel_type: str, channel_user_id: str) -> EmployeeAuthResult`
  - `im_binding_cache.bind_uid(platform, channel_user_id) -> str | None`
  - `im_binding_cache.remember_binding(*, uid, platform, channel_user_id, ttl_seconds) -> None`
  - `im_binding_cache.forget_binding(*, uid, platform) -> None`
  - `im_binding_cache.forget_user_bindings(uid) -> None`

- [ ] **Step 1: 写失败测试**
  - `SaasClient`：绑定请求方法/路径/`Authorization: AgentSession <token>`/`{channelType: "FEISHU", channelUserId}`；解绑 `DELETE .../channel-bindings/DINGTALK` 无请求体；查询返回数组；`code != 0` 抛 `SaasAPIError(code=20316)`；平台登录请求头 `X-Tenant-Admin-Key` 且不发员工 Token；管理密钥为空时不发请求直接抛 `SaasAPIError(20317)`。
  - 缓存：`remember_binding` 同时写正向与反向键且 `ex=ttl_seconds`；`bind_uid` 未命中返回 `None`；`forget_binding` 先按反向键删除正向键；键里不能出现明文 token。
- [ ] **Step 2: 运行确认失败**
  `docker compose exec -T api python -m pytest test/unit/services/test_saas_client.py test/unit/services/test_im_binding_cache.py -q` → 期望缺符号/失败。
- [ ] **Step 3: 实现**
  - `channel_type_for(platform)` 把 `feishu/dingtalk` 映射为 `FEISHU/DINGTALK`，其他平台抛 `ValueError`。
  - 统一 `{code,msg,data}` 校验沿用 `_check_response`；网络与 HTTP 异常原样抛出，由调用方映射为失败关闭。
  - 缓存键：正向 `saas:im-binding:{platform}:{channel_user_id}`，反向 `saas:im-binding:uid:{uid}:{platform}`；值只存 uid 与 channelUserId。
- [ ] **Step 4: 跑测试通过**（同 Step 2 命令）
- [ ] **Step 5: 提交**
  ```bash
  git add backend/package/yuxi/services/saas_client.py backend/package/yuxi/services/im_binding_cache.py backend/test/unit/services/test_saas_client.py backend/test/unit/services/test_im_binding_cache.py
  git commit -m "feat(im-binding): 接入 Tenant 渠道绑定接口与身份缓存原语"
  ```

### Task 9: 绑定/解绑/查询改为 Tenant 唯一保存

**Files:**
- Modify: `backend/package/yuxi/services/im_binding_service.py`
- Modify: `backend/server/routers/im_binding_router.py`
- Test: `backend/test/integration/services/test_im_binding_service.py`、`backend/test/integration/api/test_im_binding_router.py`

**Interfaces:**
- Consumes: Task 8 客户端与缓存。
- Produces: `list_bindings(db, *, user) -> list[dict]`、`delete_binding(db, *, user, platform) -> None`、`complete_binding(...) -> str`（成功即 Tenant 已保存）。
- 地图：Tenant `channelType → platform` 小写；`channelUserId → platform_user_id`；`boundAt → created_at`；只返回 `feishu/dingtalk` 且 `bound=true`。

- [ ] **Step 1: 写失败测试**
  1. `complete_binding` 成功路径调用 `bind_channel_account` 并在成功后才返回；Tenant `20316` → 409 `im_account_already_bound`；Tenant `20314` → 清理会话缓存并 401 `im_binding_login_required`；Tenant 网络失败 → 502 `im_binding_tenant_unavailable`；无本地 AgentSession → 401，且不调用 Tenant。
  2. 绑定时 `require_binding=false` / state 重放 / 过期 / 平台不匹配沿用 Phase 1 断言。
  3. `list_bindings` 只映射 Tenant 结果，不读本地绑定表；`delete_binding` 调解绑并删除缓存，Tenant 未绑定仍成功。
  4. 非 SaaS（`app_config.saas_enabled=False`）时三个用户接口都返回 400 `im_binding_not_enabled`。
- [ ] **Step 2: 运行确认失败**（同文件路径 `-q`）
- [ ] **Step 3: 实现**
  - 所有用户接口先取 `get_saas_agent_session(user.uid)`；缺失即 401，绝不落本地绑定。
  - `complete_binding` 保持「先提交 used_at，再换平台身份」；换取身份后必须成功调用 Tenant 才算绑定成功，并写正向/反向缓存。
  - Tenant 错误码映射集中在一个 helper，日志只记录 code，不记录 token/密钥。
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: 提交**
  ```bash
  git add backend/package/yuxi/services/im_binding_service.py backend/server/routers/im_binding_router.py backend/test/integration/services/test_im_binding_service.py backend/test/integration/api/test_im_binding_router.py
  git commit -m "feat(im-binding): 绑定关系改由 Tenant 唯一保存"
  ```

### Task 10: 消息身份先查缓存、冷启动调 Tenant、失败关闭

**Files:**
- Modify: `backend/package/yuxi/services/im_binding_service.py`
- Test: `backend/test/integration/api/test_channel_identity_binding.py`、`backend/test/e2e/test_im_binding_flow.py`

**Interfaces:**
- Consumes: `im_binding_cache.bind_uid`、`SaasClient.authenticate_channel`。
- Produces: `resolve_channel_identity` 行为：
  - 非 SaaS → `ChannelIdentity(user=transport_user, identity_source="owner")`，不调用 Tenant，`require_binding` 不生效。
  - 缓存命中 → 直接解析本地用户，0 次 Tenant 调用。
  - 缓存未命中 → 调一次 `auth/channel`，成功后 `find_or_create_saas_user` 并回填缓存。
  - Tenant `20206` → 按 `require_binding` 分流（409 或归属人回退）。
  - Tenant 不可达/其他错误 → 502 `im_binding_unavailable`，不得回退归属人。

- [ ] **Step 1: 写失败测试**（真实 PostgreSQL + 真实 Redis；Tenant 客户端 monkeypatch）
  1. 命中缓存的已绑定 sender：`identity_source=binding`，`authenticate_channel` 调用次数为 0。
  2. 缓存未命中：调用一次 `authenticate_channel` 并把 uid 写入缓存，第二次消息不再调用。
  3. `20206` + `require_binding=true` → 409；`20206` + `false` → `owner_fallback`。
  4. Tenant 网络错误 → 502 `im_binding_unavailable`，不提交 Run。
  5. 非 SaaS 且 `require_binding=true` → 仍 `identity_source="owner"`，不触碰 Tenant。
- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**
  - 缓存命中后按 uid 查 `users`，用户不存在时视为未命中并重新认证。
  - 冷启动用户的 `employee_code` 即本地 uid；创建走 `find_or_create_saas_user`，不复制 Tenant Session。
- [ ] **Step 4: 跑测试通过**（含 `test/unit/routers/test_agent_invocation_channel_router.py`）
- [ ] **Step 5: 提交**
  ```bash
  git add backend/package/yuxi/services/im_binding_service.py backend/test/integration/api/test_channel_identity_binding.py backend/test/e2e/test_im_binding_flow.py
  git commit -m "feat(im-binding): IM 消息身份走缓存并在冷启动回查 Tenant"
  ```

### Task 11: 登录保存会话并预热缓存，移除本地绑定表

**Files:**
- Modify: `backend/server/routers/auth_router.py`（登录无条件保存 AgentSession 并预热；登出清缓存）
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`（删除 `IMAccountBinding`）
- Modify: `backend/package/yuxi/storage/postgres/manager.py`（删除建表语句）
- Modify: `backend/test/integration/storage/test_im_binding_schema.py`、`backend/test/unit/storage/test_postgres_manager_schema.py`
- Modify: `web/src/components/SettingsModal.vue`（`imAccounts` tab 只在 `saasMode` 下出现）

- [ ] **Step 1: 写失败测试**
  - 登录成功且 `selected_mcp is None` 时仍保存 `SaasAgentSession`，并调用一次 `list_channel_bindings` 填入缓存。
  - 预热失败不影响登录返回。
  - `models_business` 不再导出 `IMAccountBinding`；`ensure_business_schema` 不再创建 `im_account_bindings`，但仍创建 `im_binding_sessions` 与 `require_binding` 列。
  - 前端 `availableTabs` 在 `saasMode=false` 时不包含 `imAccounts`。
- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**
  - `SaasAgentSession.mcp_instance_id` 在无 MCP 时为 `""`。
  - 历史库已有的 `im_account_bindings` 表不删除、不读取；决策记录写明其成为无消费者残留。
- [ ] **Step 4: 跑测试通过**
- [ ] **Step 5: 提交**
  ```bash
  git add backend/server/routers/auth_router.py backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/test/integration/storage/test_im_binding_schema.py backend/test/unit/storage/test_postgres_manager_schema.py web/src/components/SettingsModal.vue
  git commit -m "refactor(im-binding): 登录预热绑定缓存并移除本地绑定表"
  ```

### Task 12: 文档、完整验证与复审

**Files:**
- Modify: `docs/develop-guides/decisions/implemented/2026-11-01-im-account-binding.md`
- Modify: `docs/develop-guides/changelog.md`
- Modify: `docs/superpowers/plans/2026-11-01-im-account-binding.md`

- [ ] **Step 1: 改写决策记录**：Tenant 唯一保存、Redis 缓存与 TTL、失败语义、非 SaaS 停用、历史表残留、验证命令与未验证范围。
- [ ] **Step 2: 更新 changelog 原条目**：不新增重复条目。
- [ ] **Step 3: 跑完整验证**：unit + 相关 integration + e2e（可跳过范围要记录）+ 网关 + 前端 + 工程契约。
- [ ] **Step 4: 全新 Reviewer 复审**，修复 Critical/Important。
- [ ] **Step 5: 提交**
  ```bash
  git add docs/develop-guides/decisions docs/develop-guides/changelog.md docs/superpowers/plans
  git commit -m "docs(im-binding): 记录 Tenant 唯一保存与缓存语义"
  ```
