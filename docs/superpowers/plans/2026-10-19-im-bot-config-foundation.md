# IM 机器人配置底座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让管理员能在 Yuxi 后台完整配置飞书 / 钉钉机器人（凭证、归属用户、目标 Agent、白名单、群策略），并让网关通过内部接口拉到可用配置，同时补齐 Channel 入口的审批拒绝通道。

**Architecture:** 沿用现有 `models_business` + `repositories` + `services` + `server/routers` 分层。凭证与自动创建的 API Key 用对称加密存储（新 `crypto_utils`）；保存机器人时按属主自动创建绑定该用户的 `APIKey` 并把明文密文随机器人保存；网关配置通过静态服务令牌的内部接口拉取。前端在现有设置弹窗新增一个 Tab。

**Tech Stack:** FastAPI + SQLAlchemy(async) + PostgreSQL + `cryptography.Fernet`；Vue 3 + Ant Design Vue + less + lucide-vue-next（pnpm）；测试 `pytest`（backend）与 `node --test`（web）。

**Spec:** `docs/vibe/2026-10-19-feishu-dingtalk-gateway-design.md`

## Global Constraints

- Python `>=3.12,<3.14`；后端 Lint 用 Ruff，`line-length = 120`，规则 `F,E,W,UP`。
- `make format` / `make lint` 只覆盖 `backend/package` 与 `web`；`backend/server` 下新增的 Python 只靠测试与手工检查。
- 新增函数 / 类必须有清晰凝练的**中文 docstring**；关键位置补充注释。
- 测试目录：纯逻辑放 `backend/test/unit`，真实接口放 `backend/test/integration`；命名 `test_<domain>_<target>.py`，用例 `test_<行为>_<预期>`。
- 前端：API 一律定义在 `web/src/apis`；icon 优先 `lucide-vue-next`；样式用 less 且颜色取自 `web/src/assets/css/base.css` 变量；UI 组件用 Ant Design Vue（`a-*` 全局注册）。
- 实现遵循最小改动、不过度设计；不顺手重构无关代码。
- 完成后更新 `docs/develop-guides/changelog.md`。
- 测试命令统一在容器内执行：`docker compose exec api uv run --group test pytest <path> -v`；前端 `cd web && pnpm test:unit`。

## Review Focus

- **凭证不外泄**：`IMBotConfig.to_dict()` 与后台列表 / 详情响应绝不能出现 `credentials_encrypted` / `api_key_cipher` / 明文 key。
- **属主变更事务性**：更换 `owner_uid` 必须删除旧 API Key 并重建，不能残留旧 Key 或出现两个有效 Key。
- **部分更新不误删**：`PUT` 只改配置字段时，必须保留原凭证密文与 API Key（未提供 `credentials` 就不重新加密）。
- **密码学失败要显式**：加密密钥缺失或密文损坏时抛明确错误，不静默回退成明文。
- **内部接口鉴权**：`IM_GATEWAY_TOKEN` 未配置时返回 404 隐藏路由；令牌比较用常量时间；令牌错误返回 401。
- **白名单健壮性**：`allowed_users` 去重、只存字符串；空列表语义为"允许所有人"（在文档与前端提示中说明）。

---

### Task 1: 凭证加解密工具

**Files:**
- Create: `backend/package/yuxi/utils/crypto_utils.py`
- Create: `backend/test/unit/utils/test_crypto_utils.py`
- Modify: `.env.template`（新增可选 `IM_CREDENTIAL_ENCRYPTION_KEY=`）

**Interfaces:**
- Produces: `encrypt_secret(plaintext: str) -> str`、`decrypt_secret(token: str) -> str`（模块级函数，`yuxi.utils.crypto_utils`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/test/unit/utils/test_crypto_utils.py
import pytest

from yuxi.utils.crypto_utils import decrypt_secret, encrypt_secret


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")
    token = encrypt_secret("secret-value")
    assert token != "secret-value"
    assert decrypt_secret(token) == "secret-value"


def test_encrypt_is_randomized(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")
    assert encrypt_secret("x") != encrypt_secret("x")


def test_decrypt_rejects_tampered_token(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")
    with pytest.raises(Exception):
        decrypt_secret("not-a-valid-token")
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/unit/utils/test_crypto_utils.py -v`
Expected: FAIL（`ModuleNotFoundError: yuxi.utils.crypto_utils`）

- [ ] **Step 3: 实现工具**

```python
# backend/package/yuxi/utils/crypto_utils.py
"""IM 机器人凭证与 API Key 的对称加解密工具。

使用 Fernet 做可逆加密；密钥优先取 IM_CREDENTIAL_ENCRYPTION_KEY，
未配置时回退到 JWT_SECRET_KEY（复用其开发环境自动生成 / 生产必填的语义）。
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    """构建基于配置密钥的 Fernet 实例。"""
    secret = os.environ.get("IM_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not secret:
        from yuxi.utils.auth_utils import _get_jwt_secret_key

        secret = _get_jwt_secret_key()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    """加密明文，返回可存库的密文字符串。"""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """解密密文；密文损坏时由 Fernet 抛出 InvalidToken。"""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
```

- [ ] **Step 4: `.env.template` 增加可选配置**

在 JWT 配置块附近加入：

```
# IM 机器人凭证加密密钥（可选；留空时回退 JWT_SECRET_KEY）
IM_CREDENTIAL_ENCRYPTION_KEY=
```

- [ ] **Step 5: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/unit/utils/test_crypto_utils.py -v`
Expected: PASS（3 passed）

- [ ] **Step 6: 提交**

```bash
git add backend/package/yuxi/utils/crypto_utils.py backend/test/unit/utils/test_crypto_utils.py .env.template
git commit -m "feat: 新增 IM 机器人凭证加解密工具"
```

---

### Task 2: IMBotConfig 数据模型与建表

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`
- Modify: `backend/package/yuxi/storage/postgres/manager.py`（`ensure_business_schema()` 的 SQL 列表）
- Create: `backend/test/unit/storage/test_im_bot_model.py`

**Interfaces:**
- Produces: `IMBotConfig`（表 `im_bot_configs`），字段 `platform, name, enabled, credentials_encrypted, owner_uid, api_key_id, api_key_cipher, agent_slug, allowed_users, require_mention, group_sessions_per_user, created_by, updated_by, created_at, updated_at`；方法 `to_dict() -> dict`（**不含**密文字段）。

- [ ] **Step 1: 写失败测试**

```python
# backend/test/unit/storage/test_im_bot_model.py
from yuxi.storage.postgres.models_business import IMBotConfig


def test_im_bot_to_dict_hides_secrets_and_defaults_allowed_users():
    bot = IMBotConfig(
        platform="feishu",
        name="demo",
        owner_uid="u1",
        agent_slug="default-chatbot",
        credentials_encrypted="enc",
        api_key_cipher="cipher",
        allowed_users=None,
    )
    data = bot.to_dict()
    assert data["platform"] == "feishu"
    assert data["allowed_users"] == []
    assert data["require_mention"] is True
    assert data["group_sessions_per_user"] is True
    assert "credentials_encrypted" not in data
    assert "api_key_cipher" not in data
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/unit/storage/test_im_bot_model.py -v`
Expected: FAIL（`ImportError: cannot import name 'IMBotConfig'`）

- [ ] **Step 3: 新增模型**

在 `models_business.py` 末尾（其它模型之后）新增，`Text` 需要在文件顶部 import 中已存在（若无则加入 `Text`）：

```python
class IMBotConfig(Base):
    """IM 机器人配置（飞书 / 钉钉）。"""

    __tablename__ = "im_bot_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(16), nullable=False)
    name = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    credentials_encrypted = Column(Text, nullable=False)
    owner_uid = Column(String(64), nullable=False, index=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=True)
    api_key_cipher = Column(Text, nullable=True)
    agent_slug = Column(String(100), nullable=False)
    allowed_users = Column(JSON, nullable=False, default=list)
    require_mention = Column(Boolean, nullable=False, default=True)
    group_sessions_per_user = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(100), nullable=True)
    updated_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utc_now_naive)
    updated_at = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)

    def to_dict(self) -> dict[str, Any]:
        """序列化机器人配置，绝不包含凭证或 API Key 密文。"""
        return {
            "id": self.id,
            "platform": self.platform,
            "name": self.name,
            "enabled": bool(self.enabled),
            "owner_uid": self.owner_uid,
            "api_key_id": self.api_key_id,
            "agent_slug": self.agent_slug,
            "allowed_users": list(self.allowed_users or []),
            "require_mention": bool(self.require_mention),
            "group_sessions_per_user": bool(self.group_sessions_per_user),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": format_utc_datetime(self.created_at),
            "updated_at": format_utc_datetime(self.updated_at),
        }
```

- [ ] **Step 4: 补建表 DDL**

在 `manager.py` 的 `ensure_business_schema()` SQL 列表中，紧跟 `model_providers` 的建表语句后追加：

```python
            """
            CREATE TABLE IF NOT EXISTS im_bot_configs (
                id SERIAL PRIMARY KEY,
                platform VARCHAR(16) NOT NULL,
                name VARCHAR(100) NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                credentials_encrypted TEXT NOT NULL,
                owner_uid VARCHAR(64) NOT NULL,
                api_key_id INTEGER REFERENCES api_keys(id),
                api_key_cipher TEXT,
                agent_slug VARCHAR(100) NOT NULL,
                allowed_users JSONB NOT NULL DEFAULT '[]'::jsonb,
                require_mention BOOLEAN NOT NULL DEFAULT TRUE,
                group_sessions_per_user BOOLEAN NOT NULL DEFAULT TRUE,
                created_by VARCHAR(100),
                updated_by VARCHAR(100),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_im_bot_configs_owner_uid ON im_bot_configs(owner_uid)",
```

- [ ] **Step 5: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/unit/storage/test_im_bot_model.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/test/unit/storage/test_im_bot_model.py
git commit -m "feat: 新增 IM 机器人配置数据模型与建表"
```

---

### Task 3: 机器人配置服务（CRUD + API Key 自动创建 + 凭证加解密）

**Files:**
- Create: `backend/package/yuxi/repositories/im_bot_repository.py`
- Create: `backend/package/yuxi/services/im_bot_service.py`
- Create: `backend/test/unit/services/test_im_bot_service.py`

**Interfaces:**
- Consumes: `encrypt_secret` / `decrypt_secret`（Task 1）；`IMBotConfig`（Task 2）；`AuthUtils.generate_api_key()`；`APIKey`、`User` 模型。
- Produces（均定义在 `yuxi.services.im_bot_service`）：
  - `PLATFORMS: set[str]`
  - `encrypt_credentials(platform: str, credentials: dict) -> str`
  - `decrypt_credentials(platform: str, cipher: str) -> dict`
  - `async create_im_bot(db, payload: dict, username: str) -> IMBotConfig`
  - `async update_im_bot(db, bot_id: int, payload: dict, username: str) -> IMBotConfig`
  - `async delete_im_bot(db, bot_id: int) -> bool`
  - `async set_im_bot_enabled(db, bot_id: int, enabled: bool, username: str) -> IMBotConfig`
  - `async list_im_bots(db) -> list[dict]`
  - `async get_gateway_configs(db) -> list[dict]`
- 约定：所有写操作只 `flush()`，`commit()` 由 router 负责；领域错误用 `ValueError`。

- [ ] **Step 1: 写失败测试（纯逻辑部分）**

```python
# backend/test/unit/services/test_im_bot_service.py
import pytest

from yuxi.services.im_bot_service import decrypt_credentials, encrypt_credentials


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")


def test_dingtalk_credentials_round_trip():
    cipher = encrypt_credentials("dingtalk", {"client_id": "a", "client_secret": "b"})
    assert decrypt_credentials("dingtalk", cipher) == {"client_id": "a", "client_secret": "b"}


def test_feishu_credentials_default_domain():
    cipher = encrypt_credentials("feishu", {"app_id": "a", "app_secret": "b"})
    assert decrypt_credentials("feishu", cipher)["domain"] == "feishu"


def test_credentials_require_fields():
    with pytest.raises(ValueError):
        encrypt_credentials("feishu", {"app_id": "a"})
    with pytest.raises(ValueError):
        encrypt_credentials("dingtalk", {"client_id": "a"})


def test_unknown_platform_rejected():
    with pytest.raises(ValueError):
        encrypt_credentials("slack", {})
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_im_bot_service.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 repository**

```python
# backend/package/yuxi/repositories/im_bot_repository.py
"""IM 机器人配置数据访问层。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import IMBotConfig


async def list_im_bots(db: AsyncSession) -> list[IMBotConfig]:
    """按 id 升序返回全部机器人配置。"""
    result = await db.execute(select(IMBotConfig).order_by(IMBotConfig.id.asc()))
    return list(result.scalars().all())


async def get_im_bot(db: AsyncSession, bot_id: int) -> IMBotConfig | None:
    """按 id 获取机器人配置。"""
    result = await db.execute(select(IMBotConfig).where(IMBotConfig.id == bot_id))
    return result.scalar_one_or_none()


async def create_im_bot(db: AsyncSession, data: dict) -> IMBotConfig:
    """创建机器人配置（只 flush，提交交给调用方）。"""
    bot = IMBotConfig(**data)
    db.add(bot)
    await db.flush()
    await db.refresh(bot)
    return bot


async def delete_im_bot(db: AsyncSession, bot: IMBotConfig) -> None:
    """删除机器人配置。"""
    await db.delete(bot)
    await db.flush()
```

- [ ] **Step 4: 实现 service**

```python
# backend/package/yuxi/services/im_bot_service.py
"""IM 机器人配置服务：CRUD、凭证加解密与 API Key 自动创建。"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.im_bot_repository import create_im_bot as _create, delete_im_bot as _delete, get_im_bot, list_im_bots as _list
from yuxi.storage.postgres.models_business import APIKey, IMBotConfig, User
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.crypto_utils import decrypt_secret, encrypt_secret

PLATFORMS = {"feishu", "dingtalk"}

_REQUIRED_FIELDS = {
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
}


def _validate_credentials(platform: str, credentials: dict) -> dict:
    """校验平台凭证字段，返回规范化后的副本。"""
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")
    if not isinstance(credentials, dict):
        raise ValueError("credentials 必须是对象")
    normalized = {key: str(value).strip() for key, value in credentials.items() if value is not None}
    for field in _REQUIRED_FIELDS[platform]:
        if not normalized.get(field):
            raise ValueError(f"{platform} 凭证缺少 {field}")
    if platform == "feishu":
        normalized["domain"] = normalized.get("domain") or "feishu"
    return normalized


def encrypt_credentials(platform: str, credentials: dict) -> str:
    """校验并加密平台凭证，返回密文。"""
    normalized = _validate_credentials(platform, credentials)
    return encrypt_secret(json.dumps(normalized, ensure_ascii=False))


def decrypt_credentials(platform: str, cipher: str) -> dict:
    """解密密文并返回平台凭证。"""
    return json.loads(decrypt_secret(cipher))


def _normalize_allowed_users(value) -> list[str]:
    """去重并只保留非空字符串。"""
    if not value:
        return []
    seen: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


async def _resolve_owner(db: AsyncSession, owner_uid: str) -> User:
    """按 uid 找到未删除的属主用户。"""
    owner_uid = str(owner_uid or "").strip()
    if not owner_uid:
        raise ValueError("owner_uid 不能为空")
    result = await db.execute(select(User).where(User.uid == owner_uid, User.is_deleted.is_(False)))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError("归属用户不存在")
    return user


async def _provision_api_key(db: AsyncSession, user: User, bot_name: str) -> tuple[APIKey, str]:
    """为机器人创建绑定属主的 API Key，返回 (记录, 明文)。"""
    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=f"im-bot:{bot_name}",
        user_id=user.id,
        created_by=str(user.id),
    )
    db.add(api_key)
    await db.flush()
    return api_key, full_key


async def _revoke_api_key(db: AsyncSession, api_key_id: int | None) -> None:
    """删除机器人关联的 API Key（若存在）。"""
    if not api_key_id:
        return
    result = await db.execute(select(APIKey).where(APIKey.id == api_key_id))
    api_key = result.scalar_one_or_none()
    if api_key is not None:
        await db.delete(api_key)
        await db.flush()


async def create_im_bot(db: AsyncSession, payload: dict, username: str) -> IMBotConfig:
    """创建机器人配置，并自动创建绑定属主的 API Key。"""
    platform = str(payload.get("platform", "")).strip().lower()
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("name 不能为空")
    agent_slug = str(payload.get("agent_slug", "")).strip()
    if not agent_slug:
        raise ValueError("agent_slug 不能为空")
    owner = await _resolve_owner(db, payload.get("owner_uid"))
    credentials_encrypted = encrypt_credentials(platform, payload.get("credentials") or {})
    api_key, full_key = await _provision_api_key(db, owner, name)
    bot = await _create(
        db,
        {
            "platform": platform,
            "name": name,
            "enabled": bool(payload.get("enabled", True)),
            "credentials_encrypted": credentials_encrypted,
            "owner_uid": owner.uid,
            "api_key_id": api_key.id,
            "api_key_cipher": encrypt_secret(full_key),
            "agent_slug": agent_slug,
            "allowed_users": _normalize_allowed_users(payload.get("allowed_users")),
            "require_mention": bool(payload.get("require_mention", True)),
            "group_sessions_per_user": bool(payload.get("group_sessions_per_user", True)),
            "created_by": username,
            "updated_by": username,
        },
    )
    return bot


async def update_im_bot(db: AsyncSession, bot_id: int, payload: dict, username: str) -> IMBotConfig:
    """更新机器人配置；仅在提供 credentials 或 owner_uid 变化时改动凭证。"""
    bot = await get_im_bot(db, bot_id)
    if bot is None:
        raise ValueError("机器人配置不存在")

    if "owner_uid" in payload:
        owner = await _resolve_owner(db, payload.get("owner_uid"))
        if owner.uid != bot.owner_uid:
            await _revoke_api_key(db, bot.api_key_id)
            api_key, full_key = await _provision_api_key(db, owner, bot.name)
            bot.owner_uid = owner.uid
            bot.api_key_id = api_key.id
            bot.api_key_cipher = encrypt_secret(full_key)
    if "credentials" in payload:
        bot.credentials_encrypted = encrypt_credentials(bot.platform, payload.get("credentials") or {})
    if "name" in payload:
        bot.name = str(payload.get("name")).strip() or bot.name
    if "agent_slug" in payload:
        bot.agent_slug = str(payload.get("agent_slug")).strip() or bot.agent_slug
    if "enabled" in payload:
        bot.enabled = bool(payload.get("enabled"))
    if "allowed_users" in payload:
        bot.allowed_users = _normalize_allowed_users(payload.get("allowed_users"))
    if "require_mention" in payload:
        bot.require_mention = bool(payload.get("require_mention"))
    if "group_sessions_per_user" in payload:
        bot.group_sessions_per_user = bool(payload.get("group_sessions_per_user"))
    bot.updated_by = username
    await db.flush()
    await db.refresh(bot)
    return bot


async def set_im_bot_enabled(db: AsyncSession, bot_id: int, enabled: bool, username: str) -> IMBotConfig:
    """启用 / 停用机器人。"""
    bot = await get_im_bot(db, bot_id)
    if bot is None:
        raise ValueError("机器人配置不存在")
    bot.enabled = bool(enabled)
    bot.updated_by = username
    await db.flush()
    await db.refresh(bot)
    return bot


async def delete_im_bot(db: AsyncSession, bot_id: int) -> bool:
    """删除机器人配置并回收其 API Key。"""
    bot = await get_im_bot(db, bot_id)
    if bot is None:
        return False
    await _revoke_api_key(db, bot.api_key_id)
    await _delete(db, bot)
    return True


async def list_im_bots(db: AsyncSession) -> list[dict]:
    """返回全部机器人配置的序列化结果。"""
    bots = await _list(db)
    return [bot.to_dict() for bot in bots]


async def get_gateway_configs(db: AsyncSession) -> list[dict]:
    """返回启用中的机器人配置，含解密后的凭证与 API Key 明文，供网关使用。"""
    configs = []
    for bot in await _list(db):
        if not bot.enabled:
            continue
        if not bot.api_key_cipher:
            continue
        configs.append(
            {
                "id": bot.id,
                "platform": bot.platform,
                "name": bot.name,
                "credentials": decrypt_credentials(bot.platform, bot.credentials_encrypted),
                "api_key": decrypt_secret(bot.api_key_cipher),
                "agent_slug": bot.agent_slug,
                "allowed_users": list(bot.allowed_users or []),
                "require_mention": bool(bot.require_mention),
                "group_sessions_per_user": bool(bot.group_sessions_per_user),
            }
        )
    return configs
```

- [ ] **Step 5: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_im_bot_service.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/package/yuxi/repositories/im_bot_repository.py backend/package/yuxi/services/im_bot_service.py backend/test/unit/services/test_im_bot_service.py
git commit -m "feat: 新增 IM 机器人配置服务与 API Key 自动创建"
```

---

### Task 4: 后台 CRUD 接口与网关配置拉取接口

**Files:**
- Create: `backend/server/routers/im_bot_router.py`
- Modify: `backend/package/yuxi/config/app.py`（新增 `im_gateway_token` 字段 + env 映射）
- Modify: `.env.template`（新增可选 `IM_GATEWAY_TOKEN=`）
- Modify: `backend/server/routers/__init__.py`
- Create: `backend/test/integration/api/test_im_bot_router.py`

**Interfaces:**
- Consumes: Task 3 的 service 函数。
- Produces:
  - `im_bots = APIRouter(prefix="/system/im-bots", tags=["im-bots"])`（管理员 CRUD）
  - `im_gateway = APIRouter(prefix="/internal/im-gateway", tags=["im-gateway"])`（`GET /config`，`X-IM-Gateway-Token` 鉴权）
  - `require_gateway_token`（`Header(alias="X-IM-Gateway-Token")`，常量时间比较）

- [ ] **Step 1: 写失败测试（集成）**

```python
# backend/test/integration/api/test_im_bot_router.py
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _an_agent_slug(test_client, headers) -> str:
    resp = await test_client.get("/api/agent", headers=headers)
    assert resp.status_code == 200, resp.text
    agent = resp.json()["agents"][0]
    return agent.get("slug") or agent.get("agent_id")


async def _admin_uid(test_client, headers) -> str:
    resp = await test_client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["uid"]


def _payload(uid: str, agent_slug: str) -> dict:
    return {
        "platform": "feishu",
        "name": f"pytest-bot-{uuid.uuid4().hex[:8]}",
        "owner_uid": uid,
        "agent_slug": agent_slug,
        "credentials": {"app_id": "cli_test", "app_secret": "secret_test"},
        "allowed_users": ["ou_a", "ou_a", " ou_b "],
        "require_mention": True,
        "group_sessions_per_user": True,
    }


async def test_create_list_update_delete_im_bot(test_client, admin_headers):
    uid = await _admin_uid(test_client, admin_headers)
    slug = await _an_agent_slug(test_client, admin_headers)

    created = await test_client.post("/api/system/im-bots", json=_payload(uid, slug), headers=admin_headers)
    assert created.status_code == 200, created.text
    bot = created.json()["data"]
    assert bot["api_key_id"] is not None
    assert bot["allowed_users"] == ["ou_a", "ou_b"]
    assert "credentials_encrypted" not in bot
    bot_id = bot["id"]

    listed = await test_client.get("/api/system/im-bots", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == bot_id for item in listed.json()["data"])

    updated = await test_client.put(
        f"/api/system/im-bots/{bot_id}",
        json={"agent_slug": slug, "enabled": False},
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["enabled"] is False
    assert updated.json()["data"]["api_key_id"] == bot["api_key_id"]

    deleted = await test_client.delete(f"/api/system/im-bots/{bot_id}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text


async def test_gateway_config_requires_token(test_client, admin_headers):
    token = os.environ.get("IM_GATEWAY_TOKEN", "")
    if not token:
        pytest.skip("IM_GATEWAY_TOKEN 未配置，跳过网关配置接口集成测试")

    uid = await _admin_uid(test_client, admin_headers)
    slug = await _an_agent_slug(test_client, admin_headers)
    created = await test_client.post("/api/system/im-bots", json=_payload(uid, slug), headers=admin_headers)
    assert created.status_code == 200, created.text
    bot_id = created.json()["data"]["id"]

    denied = await test_client.get("/api/internal/im-gateway/config", headers={"X-IM-Gateway-Token": "wrong"})
    assert denied.status_code == 401

    ok = await test_client.get("/api/internal/im-gateway/config", headers={"X-IM-Gateway-Token": token})
    assert ok.status_code == 200, ok.text
    entry = next(item for item in ok.json()["data"] if item["id"] == bot_id)
    assert entry["api_key"].startswith("yxkey_")
    assert entry["credentials"]["app_id"] == "cli_test"

    await test_client.delete(f"/api/system/im-bots/{bot_id}", headers=admin_headers)
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/integration/api/test_im_bot_router.py -v`
Expected: FAIL（404，路由不存在）

- [ ] **Step 3: 新增 config 字段**

在 `backend/package/yuxi/config/app.py` 的 `Config` 中 `tenant_admin_api_key` 附近加入：

```python
    im_gateway_token: str = Field(default="", description="IM 网关拉取机器人配置的静态服务令牌；未配置时内部接口返回 404")
```

并在 `_apply_env_defaults()` 的映射列表中加一行：

```python
            ("im_gateway_token", "IM_GATEWAY_TOKEN"),
```

- [ ] **Step 4: 实现 router**

```python
# backend/server/routers/im_bot_router.py
"""IM 机器人配置管理接口与网关配置拉取接口。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.services.im_bot_service import (
    create_im_bot,
    delete_im_bot,
    list_im_bots,
    get_gateway_configs,
    set_im_bot_enabled,
    update_im_bot,
)
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_admin_user, get_db

im_bots = APIRouter(prefix="/system/im-bots", tags=["im-bots"])
im_gateway = APIRouter(prefix="/internal/im-gateway", tags=["im-gateway"])


class IMBotPayload(BaseModel):
    """机器人创建 / 更新请求体。"""

    platform: str | None = Field(None, description="feishu 或 dingtalk")
    name: str | None = None
    enabled: bool | None = None
    owner_uid: str | None = None
    agent_slug: str | None = None
    credentials: dict | None = None
    allowed_users: list[str] | None = None
    require_mention: bool | None = None
    group_sessions_per_user: bool | None = None


def require_gateway_token(x_im_gateway_token: str | None = Header(None, alias="X-IM-Gateway-Token")) -> None:
    """校验网关静态令牌；未配置时隐藏路由。"""
    from yuxi.config import config as app_config

    token = app_config.im_gateway_token
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_im_gateway_token or not hmac.compare_digest(x_im_gateway_token, token):
        raise HTTPException(status_code=401, detail="无效的网关令牌")


@im_bots.get("")
async def list_bots(current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """列出全部机器人配置。"""
    return {"success": True, "data": await list_im_bots(db)}


@im_bots.post("")
async def create_bot(
    payload: IMBotPayload,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建机器人配置。"""
    try:
        bot = await create_im_bot(db, payload.model_dump(exclude_none=True), current_user.username)
        await db.commit()
        return {"success": True, "data": bot.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@im_bots.put("/{bot_id}")
async def update_bot(
    bot_id: int,
    payload: IMBotPayload,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新机器人配置。"""
    try:
        bot = await update_im_bot(db, bot_id, payload.model_dump(exclude_unset=True), current_user.username)
        await db.commit()
        return {"success": True, "data": bot.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@im_bots.post("/{bot_id}/enabled")
async def set_enabled(
    bot_id: int,
    enabled: bool,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """启用或停用机器人。"""
    try:
        bot = await set_im_bot_enabled(db, bot_id, enabled, current_user.username)
        await db.commit()
        return {"success": True, "data": bot.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@im_bots.delete("/{bot_id}")
async def delete_bot(
    bot_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """删除机器人配置。"""
    deleted = await delete_im_bot(db, bot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="机器人配置不存在")
    await db.commit()
    return {"success": True}


@im_gateway.get("/config")
async def gateway_config(_: None = Depends(require_gateway_token), db: AsyncSession = Depends(get_db)):
    """返回启用中的机器人配置（含解密凭证），供网关拉取。"""
    return {"success": True, "data": await get_gateway_configs(db)}
```

- [ ] **Step 5: 注册路由**

在 `backend/server/routers/__init__.py` 顶部 import 区加入：

```python
from server.routers.im_bot_router import im_bots, im_gateway
```

在 `router.include_router(model_providers)` 附近加入：

```python
router.include_router(im_bots)  # /api/system/im-bots/*
router.include_router(im_gateway)  # /api/internal/im-gateway/*
```

- [ ] **Step 6: `.env.template` 新增令牌**

```
# IM 网关凭据（可选；未配置时 /api/internal/im-gateway/* 返回 404）
IM_GATEWAY_TOKEN=
```

- [ ] **Step 7: 运行确认通过**

先重启 api 让新路由与静态资源生效（热重载通常自动完成）：`docker compose restart api`
Run: `docker compose exec api uv run --group test pytest test/integration/api/test_im_bot_router.py -v`
Expected: PASS（或在未配置 `IM_GATEWAY_TOKEN` 时第二个用例 skip）

- [ ] **Step 8: 提交**

```bash
git add backend/server/routers/im_bot_router.py backend/server/routers/__init__.py backend/package/yuxi/config/app.py .env.template backend/test/integration/api/test_im_bot_router.py
git commit -m "feat: 新增 IM 机器人配置管理与网关配置拉取接口"
```

---

### Task 5: Channel 入口的审批拒绝通道

**Files:**
- Modify: `backend/server/routers/agent_invocation_channel_router.py`
- Create: `backend/test/unit/routers/test_agent_invocation_channel_decisions.py`
- Modify: `backend/test/integration/api/test_agent_invocation_channel_api.py`

**Interfaces:**
- Produces: `decision_type_for_command(name: str, args: tuple[str, ...]) -> str | None`（`"approve"` / `"reject"` / `None`）。
- 行为变更：`/approve` 语义不变；新增 `/reject` 走同一恢复逻辑，decision 类型为 `reject`。

- [ ] **Step 1: 写失败测试（单元）**

```python
# backend/test/unit/routers/test_agent_invocation_channel_decisions.py
import pytest

from server.routers.agent_invocation_channel_router import decision_type_for_command


def test_approve_maps_to_approve():
    assert decision_type_for_command("approve", ()) == "approve"


def test_reject_maps_to_reject():
    assert decision_type_for_command("reject", ()) == "reject"


def test_other_commands_return_none():
    assert decision_type_for_command("state", ()) is None


def test_decision_command_rejects_args():
    with pytest.raises(ValueError):
        decision_type_for_command("reject", ("x",))
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/unit/routers/test_agent_invocation_channel_decisions.py -v`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 重构 Channel router**

在 `agent_invocation_channel_router.py` 中新增纯函数：

```python
_DECISION_COMMANDS = {"approve": "approve", "reject": "reject"}


def decision_type_for_command(name: str, args: tuple[str, ...]) -> str | None:
    """将审批类 slash command 映射为 decision 类型；非审批命令返回 None。"""
    decision = _DECISION_COMMANDS.get(name)
    if decision is None:
        return None
    _require_no_args(name, args)
    return decision
```

把 `receive_channel_message` 中的审批分支替换为：

```python
    if command is not None:
        if command.name == "state":
            _require_no_args(command.name, command.args)
            state = await get_agent_state_view(
                thread_id=thread_id,
                current_user=current_user,
                db=db,
                include_messages=False,
            )
            return {"kind": "command", "command": "state", "thread_id": thread_id, "state": state}
        try:
            decision = decision_type_for_command(command.name, command.args)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if decision is not None:
            return await _resolve_latest_run(
                decision=decision,
                agent_slug=agent_slug,
                thread_id=thread_id,
                request_id=request_id,
                external_id=external_id,
                channel=channel,
                origin_metadata=origin_metadata,
                current_user=current_user,
                db=db,
            )
        raise HTTPException(status_code=422, detail=f"不支持的 slash command: /{command.name}")
```

把 `_approve_latest_run` 重命名为 `_resolve_latest_run`，签名加 `decision: str`，并把两处 `{"type": "approve"}` 改成 `{"type": decision}`，返回的 `command` 字段改为 `decision`，docstring 更新为"审批（批准或拒绝）当前等待中的工具调用"。

- [ ] **Step 4: 写集成测试（拒绝路径）**

在 `backend/test/integration/api/test_agent_invocation_channel_api.py` 末尾追加：

```python
async def test_channel_reject_without_pending_approval_returns_conflict(test_client, admin_headers):
    agents_response = await test_client.get("/api/agent", headers=admin_headers)
    assert agents_response.status_code == 200, agents_response.text
    agent_slug = agents_response.json()["agents"][0].get("slug") or agents_response.json()["agents"][0].get(
        "agent_id"
    )

    thread_response = await test_client.post(
        "/api/chat/thread",
        json={"agent_id": agent_slug, "title": f"pytest-channel-reject-{uuid.uuid4().hex[:8]}", "metadata": {}},
        headers=admin_headers,
    )
    assert thread_response.status_code == 200, thread_response.text
    thread_id = thread_response.json().get("thread_id") or thread_response.json().get("id")

    response = await test_client.post(
        "/api/agent-invocation/channel/messages",
        json={
            "channel": "cli",
            "account_id": "integration",
            "chat_id": "reject-check",
            "thread_id": thread_id,
            "message_id": f"reject-{uuid.uuid4().hex}",
            "agent_slug": agent_slug,
            "message": {"type": "text", "text": "/reject"},
        },
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "no_pending_approval"
```

- [ ] **Step 5: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/unit/routers/test_agent_invocation_channel_decisions.py -v`
Run: `docker compose exec api uv run --group test pytest test/integration/api/test_agent_invocation_channel_api.py -v`
Expected: 均 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/server/routers/agent_invocation_channel_router.py backend/test/unit/routers/test_agent_invocation_channel_decisions.py backend/test/integration/api/test_agent_invocation_channel_api.py
git commit -m "feat: Channel 入口支持工具审批拒绝"
```

---

### Task 6: 前端 API 模块与表单映射工具

**Files:**
- Create: `web/src/apis/im_bot_api.js`
- Modify: `web/src/apis/index.js`
- Create: `web/src/utils/imBotForm.js`
- Create: `web/test/unit/imBotForm.test.js`

**Interfaces:**
- Produces:
  - `imBotApi`（`list/create/update/remove/setEnabled`）
  - `buildImBotPayload(form) -> object`、`formFromImBot(bot) -> object`（`web/src/utils/imBotForm.js`）

- [ ] **Step 1: 写失败测试**

```javascript
// web/test/unit/imBotForm.test.js
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildImBotPayload, formFromImBot, parseAllowedUsers } from '../../src/utils/imBotForm.js'

test('parseAllowedUsers splits, trims and dedupes', () => {
  assert.deepEqual(parseAllowedUsers('ou_a, ou_a\nou_b'), ['ou_a', 'ou_b'])
})

test('buildImBotPayload maps form fields', () => {
  const payload = buildImBotPayload({
    platform: 'feishu',
    name: 'bot',
    ownerUid: 'u1',
    agentSlug: 'default-chatbot',
    enabled: true,
    allowedUsersText: 'ou_a,ou_b',
    requireMention: true,
    groupSessionsPerUser: true,
    credentials: { app_id: 'a', app_secret: 'b' }
  })
  assert.equal(payload.owner_uid, 'u1')
  assert.deepEqual(payload.allowed_users, ['ou_a', 'ou_b'])
  assert.equal(payload.require_mention, true)
  assert.equal(payload.credentials.app_id, 'a')
})

test('formFromImBot omits credentials and joins users', () => {
  const form = formFromImBot({
    platform: 'dingtalk',
    name: 'bot',
    owner_uid: 'u2',
    agent_slug: 'x',
    enabled: false,
    allowed_users: ['a', 'b'],
    require_mention: false,
    group_sessions_per_user: false
  })
  assert.equal(form.ownerUid, 'u2')
  assert.equal(form.allowedUsersText, 'a, b')
  assert.equal(form.platform, 'dingtalk')
})
```

- [ ] **Step 2: 运行确认失败**

Run: `cd web && node --test test/unit/imBotForm.test.js`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现表单映射工具**

```javascript
// web/src/utils/imBotForm.js

export function parseAllowedUsers(text) {
  const seen = []
  for (const raw of String(text || '').split(/[\s,，]+/)) {
    const value = raw.trim()
    if (value && !seen.includes(value)) seen.push(value)
  }
  return seen
}

export function buildImBotPayload(form) {
  const payload = {
    platform: form.platform,
    name: form.name,
    owner_uid: form.ownerUid,
    agent_slug: form.agentSlug,
    enabled: form.enabled,
    allowed_users: parseAllowedUsers(form.allowedUsersText),
    require_mention: form.requireMention,
    group_sessions_per_user: form.groupSessionsPerUser
  }
  if (form.credentials) payload.credentials = { ...form.credentials }
  return payload
}

export function formFromImBot(bot) {
  return {
    platform: bot.platform || 'feishu',
    name: bot.name || '',
    ownerUid: bot.owner_uid || '',
    agentSlug: bot.agent_slug || '',
    enabled: bot.enabled !== false,
    allowedUsersText: (bot.allowed_users || []).join(', '),
    requireMention: bot.require_mention !== false,
    groupSessionsPerUser: bot.group_sessions_per_user !== false,
    credentials: null
  }
}
```

- [ ] **Step 4: 实现 API 模块**

```javascript
// web/src/apis/im_bot_api.js
import { apiAdminGet, apiAdminPost, apiAdminPut, apiAdminDelete } from './base'

const BASE_URL = '/api/system/im-bots'

export const imBotApi = {
  list: () => apiAdminGet(BASE_URL),
  create: (data) => apiAdminPost(BASE_URL, data),
  update: (id, data) => apiAdminPut(`${BASE_URL}/${id}`, data),
  remove: (id) => apiAdminDelete(`${BASE_URL}/${id}`),
  setEnabled: (id, enabled) => apiAdminPost(`${BASE_URL}/${id}/enabled?enabled=${enabled}`, {})
}

export default imBotApi
```

- [ ] **Step 5: 导出模块**

在 `web/src/apis/index.js` 增加：

```javascript
export * from './im_bot_api'
```

- [ ] **Step 6: 运行确认通过**

Run: `cd web && node --test test/unit/imBotForm.test.js`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add web/src/apis/im_bot_api.js web/src/apis/index.js web/src/utils/imBotForm.js web/test/unit/imBotForm.test.js
git commit -m "feat: 新增 IM 机器人前端 API 与表单映射工具"
```

---

### Task 7: 前端「IM 机器人」管理页并在设置弹窗接入

**Files:**
- Create: `web/src/components/ImBotManagementComponent.vue`
- Modify: `web/src/components/SettingsModal.vue`

**Interfaces:**
- Consumes: `imBotApi`（Task 6）、`buildImBotPayload` / `formFromImBot`（Task 6）、`authApi.getUserAccessOptions()`、`agentApi.getAgents({ includeSubagents: false })`。

- [ ] **Step 1: 新建管理组件**

参照 `DepartmentManagementComponent.vue` 的列表 + 表单弹窗写法。组件需包含：顶部刷新 / 新增按钮（`RefreshCw` / `Plus` icon）、`a-table`（列：名称、平台、目标 Agent、状态开关、操作）、新增/编辑 `a-modal`（`a-form` vertical，字段：平台 `a-select`、名称 `a-input`、归属用户 `a-select show-search`（数据来自 `getUserAccessOptions()`）、目标 Agent `a-select`（来自 `getAgents()`）、凭证（飞书 `app_id`/`app_secret`/`domain`；钉钉 `client_id`/`client_secret`，用 `a-input-password`）、白名单 `a-textarea`（提示"留空表示允许所有人"）、群策略两个 `a-switch`）、删除用 `Modal.confirm`。

组件核心状态与提交（与组件内其他部分一起写入该文件）：

```javascript
const state = reactive({
  loading: false, bots: [], error: null,
  modalVisible: false, editMode: false, editId: null,
  saving: false, form: formFromImBot({})
})

const fetchBots = async () => {
  try {
    state.loading = true
    state.error = null
    const result = await imBotApi.list()
    state.bots = result.data || []
  } catch (error) {
    console.error('获取 IM 机器人失败:', error)
    state.error = '获取 IM 机器人失败'
  } finally {
    state.loading = false
  }
}

const submit = async () => {
  state.saving = true
  try {
    const payload = buildImBotPayload(state.form)
    if (state.editMode) await imBotApi.update(state.editId, payload)
    else await imBotApi.create(payload)
    notification.success({ message: '保存成功' })
    state.modalVisible = false
    await fetchBots()
  } catch (error) {
    notification.error({ message: '保存失败', description: error.message || '请稍后重试' })
  } finally {
    state.saving = false
  }
}
```

提交前需至少校验：`name`、`ownerUid`、`agentSlug` 非空；新建时凭证必填（对应平台）。

- [ ] **Step 2: 在 SettingsModal 接入 Tab（三处 + availableTabs + import）**

在 `SettingsModal.vue`：

1) 脚本 import 区加入：

```javascript
import ImBotManagementComponent from '@/components/ImBotManagementComponent.vue'
```

2) 桌面侧栏 `.sider-sider-nav` 内、其它管理员项之后加入：

```html
<div class="sider-item" :class="{ activesec: activeTab === 'imBots' }" @click="activeTab = 'imBots'" v-if="userStore.isAdmin && !userStore.saasMode">
  <Bot class="icon" :size="18" />
  <span>IM 机器人</span>
</div>
```

并从 `lucide-vue-next` 引入 `Bot`。

3) 移动端 `.settings-mobile-nav` 内加入对应项：

```html
<div class="nav-item" :class="{ active: activeTab === 'imBots' }" @click="activeTab = 'imBots'" v-if="userStore.isAdmin && !userStore.saasMode">IM 机器人</div>
```

4) 内容区加入：

```html
<div v-show="activeTab === 'imBots'" v-if="userStore.isAdmin && !userStore.saasMode">
  <ImBotManagementComponent />
</div>
```

5) `availableTabs` 的管理员分支追加 `'imBots'`：

```javascript
if (userStore.isAdmin && !userStore.saasMode) tabs.push('base', 'ocr', 'user', 'imBots')
```

- [ ] **Step 3: 构建 / 静态检查**

Run: `cd web && pnpm run lint`
Expected: 无新增报错

- [ ] **Step 4: 手工验证**

Run: `docker compose up -d web`，打开页面 → 设置 → IM 机器人，创建一个飞书机器人（归属用户、目标 Agent、凭证、白名单），确认列表出现且开关可切换、编辑不丢凭证、删除生效。

- [ ] **Step 5: 提交**

```bash
git add web/src/components/ImBotManagementComponent.vue web/src/components/SettingsModal.vue
git commit -m "feat: 新增 IM 机器人后台管理页面"
```

---

### Task 8: 更新变更日志

**Files:**
- Modify: `docs/develop-guides/changelog.md`

- [ ] **Step 1: 追加变更条目**

在 changelog 最新一条之上新增一条，概述本次交付：新增 IM 机器人配置底座（`im_bot_configs` 表、凭证对称加密、按属主自动创建 API Key、`/api/system/im-bots` 管理接口、`/api/internal/im-gateway/config` 网关配置拉取）、Channel 入口新增工具审批拒绝通道、前端新增「IM 机器人」设置页。

- [ ] **Step 2: 提交**

```bash
git add docs/develop-guides/changelog.md
git commit -m "docs: 更新 IM 机器人配置底座变更日志"
```

---

## Self-Review

- **Spec coverage**：模型（Task 2）、加密（Task 1）、API Key 自动创建（Task 3）、后台 CRUD（Task 4）、网关配置拉取（Task 4）、审批拒绝通道（Task 5）、前端管理页（Task 6/7）、changelog（Task 8）均覆盖。spec 中"测试连接"与网关实际收发在 Plan 2（gateway）交付，本计划不含；已在 Task 4 显式省略。
- **类型一致性**：service 产出的 `encrypt_credentials` / `decrypt_credentials` 名称在 Task 3 定义、Task 4 间接使用；`require_gateway_token` 名称一致；前端 `buildImBotPayload` / `formFromImBot` 在 Task 6 定义、Task 7 使用。
- **Review Focus 对应**：密文不外泄（Task 2 测试）、属主变更事务性（Task 3 `update_im_bot` + Task 4 集成）、部分更新不误删（Task 3 `payload.get(...)` 分支 + Task 4 集成断言 `api_key_id` 不变）、加密显式失败（Task 1 `test_decrypt_rejects_tampered_token`）、内部接口鉴权（Task 4 `test_gateway_config_requires_token`）、白名单去重（Task 3 `test` + Task 4 断言）。
