# IM 网关服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增独立常驻的 `gateway` 服务，用飞书 / 钉钉的 WebSocket 长连接收发消息，把入站文本桥接到 Yuxi 的 Channel 入口，把 Agent 的 markdown 正文与 Excel 交付物回发，并用交互卡片完成工具审批闭环。

**Architecture:** 新建 Python 包 `packages/yuxi-im-gateway`（与 `packages/yuxi-cli` 同级），一个进程加载全部启用机器人，每个机器人一个平台 adapter 与独立长连接。所有与 Yuxi 的交互走 HTTP + API Key（复用 CLI 已用的 Channel / SSE / 附件接口），不直连数据库。纯逻辑（白名单、@ 判定、去重、SSE 归并、markdown 降级）与平台 SDK 解耦，便于单测。

**Tech Stack:** Python 3.13 + `httpx` + `lark-oapi`（飞书 WS）+ `dingtalk-stream`（钉钉 Stream Mode）+ `openpyxl`（仅用于把 `.md` 结果转 Excel 交付场景不需，故不引入；附件以原始字节转发）。测试 `pytest`。

**Spec:** `docs/vibe/2026-10-19-feishu-dingtalk-gateway-design.md`
**Depends on:** Plan 1（`docs/superpowers/plans/2026-10-19-im-bot-config-foundation.md`）— 提供 `/api/internal/im-gateway/config`、Channel `/reject`、`IM_GATEWAY_TOKEN`。

## Global Constraints

- 网关是独立进程 / 容器，**不**直连 Postgres / Redis，只调用 Yuxi HTTP API。
- 与 Yuxi 的每次业务调用使用机器人自己的 API Key：`Authorization: Bearer <yxkey_...>`（见 `backend/server/utils/auth_middleware.py:72`）。
- 机器人配置通过 `GET /api/internal/im-gateway/config` 拉取，请求头 `X-IM-Gateway-Token: <IM_GATEWAY_TOKEN>`。
- 每个新增函数 / 类必须有清晰凝练的**中文 docstring**；关键位置补注释。
- Ruff `line-length = 120`，规则 `F,E,W,UP`；测试目录 `packages/yuxi-im-gateway/tests/`，命名 `test_<模块>_<行为>.py`。
- `allowed_users` 为空 = 允许所有人；群聊仅在被 @ 且 `require_mention` 时响应。
- 平台 SDK 的具体 API 以实际安装版本为准；**纯逻辑不依赖 SDK**，SDK 调用只出现在 adapter 内并被 fake 替换。
- 完成后更新 `docs/develop-guides/changelog.md`。

## Review Focus

- **纯逻辑可测**：白名单 / @ 判定 / 去重 / 会话键 / SSE 归并 / markdown 降级必须是纯函数或可注入的小类，adapter 只做协议翻译。
- **审批幂等**：卡片重复点击不能产生第二个恢复 run；`request_id` 必须由 decision + 父 `run_id` 确定性生成。
- **串行与去重**：同一会话串行；平台 `message_id` 去重且有 TTL，避免内存无限增长。
- **故障隔离**：单个机器人凭证无效 / 连接失败不得影响其他机器人（逐个 try/except，记录日志）。
- **出站降级**：markdown / 富文本失败时降级为纯文本，附件失败不阻断正文。
- **凭证不落盘**：网关不从磁盘持久化凭证，只在内存中从配置接口拉取。

---

### Task 1: 网关包骨架与纯逻辑核心

**Files:**
- Create: `packages/yuxi-im-gateway/pyproject.toml`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/__init__.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/models.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/filters.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/dedupe.py`
- Create: `packages/yuxi-im-gateway/tests/test_filters.py`
- Create: `packages/yuxi-im-gateway/tests/test_dedupe.py`

**Interfaces:**
- Produces:
  - `BotConfig`（dataclass：`id, platform, name, credentials, api_key, agent_slug, allowed_users, require_mention, group_sessions_per_user`）
  - `InboundMessage`（dataclass：`message_id, chat_id, sender_id, is_group, mentions_bot, text`）
  - `is_allowed(bot: BotConfig, sender_id: str) -> bool`
  - `should_handle(bot: BotConfig, msg: InboundMessage) -> bool`
  - `session_chat_id(bot: BotConfig, msg: InboundMessage) -> str`
  - `DedupeCache(max_size: int, ttl_seconds: float)`，方法 `seen(key: str, now: float | None = None) -> bool`

- [ ] **Step 1: 写包元数据**

```toml
# packages/yuxi-im-gateway/pyproject.toml
[build-system]
requires = ["setuptools>=80", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "yuxi-im-gateway"
version = "0.1.0"
description = "Linko AI IM gateway (Feishu / DingTalk)"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "lark-oapi>=1.4.0",
    "dingtalk-stream>=0.24.0",
]

[project.scripts]
yuxi-im-gateway = "yuxi_im_gateway.main:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[dependency-groups]
test = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.21"]
```

创建空的 `README.md` 与 `src/yuxi_im_gateway/__init__.py`（内容留空或仅一行 docstring）。

- [ ] **Step 2: 写失败测试**

```python
# packages/yuxi-im-gateway/tests/test_filters.py
from yuxi_im_gateway.filters import is_allowed, session_chat_id, should_handle
from yuxi_im_gateway.models import BotConfig, InboundMessage


def _bot(**overrides) -> BotConfig:
    base = dict(
        id=1, platform="feishu", name="bot", credentials={}, api_key="yxkey_x",
        agent_slug="default-chatbot", allowed_users=[], require_mention=True,
        group_sessions_per_user=True,
    )
    base.update(overrides)
    return BotConfig(**base)


def test_empty_whitelist_allows_everyone():
    assert is_allowed(_bot(allowed_users=[]), "ou_any") is True


def test_non_empty_whitelist_requires_hit():
    bot = _bot(allowed_users=["ou_a", "ou_b"])
    assert is_allowed(bot, "ou_a") is True
    assert is_allowed(bot, "ou_c") is False


def test_private_chat_always_handled_even_without_mention():
    msg = InboundMessage("m1", "c1", "ou_a", is_group=False, mentions_bot=False, text="hi")
    assert should_handle(_bot(require_mention=True), msg) is True


def test_group_requires_mention_when_configured():
    bot = _bot(require_mention=True)
    mentioned = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=True, text="@bot hi")
    ignored = InboundMessage("m2", "c1", "ou_a", is_group=True, mentions_bot=False, text="hi")
    assert should_handle(bot, mentioned) is True
    assert should_handle(bot, ignored) is False


def test_group_without_require_mention_accepts_all():
    bot = _bot(require_mention=False)
    msg = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=False, text="hi")
    assert should_handle(bot, msg) is True


def test_group_session_is_per_user_when_enabled():
    bot = _bot(group_sessions_per_user=True)
    msg = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=True, text="x")
    assert session_chat_id(bot, msg) == "c1:ou_a"


def test_group_session_shared_when_disabled():
    bot = _bot(group_sessions_per_user=False)
    msg = InboundMessage("m1", "c1", "ou_a", is_group=True, mentions_bot=True, text="x")
    assert session_chat_id(bot, msg) == "c1"


def test_private_session_uses_chat_id():
    msg = InboundMessage("m1", "c9", "ou_a", is_group=False, mentions_bot=False, text="x")
    assert session_chat_id(_bot(), msg) == "c9"
```

```python
# packages/yuxi-im-gateway/tests/test_dedupe.py
from yuxi_im_gateway.dedupe import DedupeCache


def test_seen_marks_first_then_repeats():
    cache = DedupeCache(max_size=10, ttl_seconds=60)
    assert cache.seen("a", now=0.0) is False
    assert cache.seen("a", now=1.0) is True


def test_seen_expires_after_ttl():
    cache = DedupeCache(max_size=10, ttl_seconds=5)
    cache.seen("a", now=0.0)
    assert cache.seen("a", now=6.0) is False


def test_cache_bounds_size_by_evicting_oldest():
    cache = DedupeCache(max_size=2, ttl_seconds=100)
    cache.seen("a", now=0.0)
    cache.seen("b", now=1.0)
    cache.seen("c", now=2.0)
    assert cache.seen("a", now=2.5) is False
    assert cache.seen("c", now=2.5) is True
```

- [ ] **Step 3: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/ -v`
Expected: FAIL（模块不存在）

- [ ] **Step 4: 实现 models / filters / dedupe**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/models.py
"""网关内部使用的归一化数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class BotConfig:
    """一个 IM 机器人配置（网关侧视图）。"""

    id: int
    platform: str
    name: str
    credentials: dict
    api_key: str
    agent_slug: str
    allowed_users: list[str] = field(default_factory=list)
    require_mention: bool = True
    group_sessions_per_user: bool = True

    @classmethod
    def from_payload(cls, payload: dict) -> "BotConfig":
        """从后台配置接口返回项构造。"""
        return cls(
            id=int(payload["id"]),
            platform=str(payload["platform"]),
            name=str(payload.get("name") or ""),
            credentials=dict(payload.get("credentials") or {}),
            api_key=str(payload.get("api_key") or ""),
            agent_slug=str(payload.get("agent_slug") or ""),
            allowed_users=[str(item) for item in (payload.get("allowed_users") or [])],
            require_mention=bool(payload.get("require_mention", True)),
            group_sessions_per_user=bool(payload.get("group_sessions_per_user", True)),
        )


@dataclass(slots=True)
class InboundMessage:
    """平台事件归一化后的入站消息。"""

    message_id: str
    chat_id: str
    sender_id: str
    is_group: bool
    mentions_bot: bool
    text: str
```

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/filters.py
"""入站消息的白名单、@ 判定与会话键计算。"""

from __future__ import annotations

from yuxi_im_gateway.models import BotConfig, InboundMessage


def is_allowed(bot: BotConfig, sender_id: str) -> bool:
    """白名单为空表示允许所有人；否则发送者必须命中。"""
    if not bot.allowed_users:
        return True
    return sender_id in bot.allowed_users


def should_handle(bot: BotConfig, msg: InboundMessage) -> bool:
    """判断是否应处理该消息：私聊直接处理，群聊按 @ 规则。"""
    if not msg.is_group:
        return True
    if not bot.require_mention:
        return True
    return msg.mentions_bot


def session_chat_id(bot: BotConfig, msg: InboundMessage) -> str:
    """计算传给 Channel 的 chat_id，群内按用户隔离时拼接发送者。"""
    if msg.is_group and bot.group_sessions_per_user:
        return f"{msg.chat_id}:{msg.sender_id}"
    return msg.chat_id
```

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/dedupe.py
"""带 TTL 与容量上限的消息去重缓存。"""

from __future__ import annotations

import time
from collections import OrderedDict


class DedupeCache:
    """按 key 记录最近处理时间，避免重复处理同一条平台消息。"""

    def __init__(self, max_size: int = 4096, ttl_seconds: float = 3600) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()

    def seen(self, key: str, now: float | None = None) -> bool:
        """返回该 key 是否在 TTL 内已见过；未见则记录并返回 False。"""
        current = time.monotonic() if now is None else now
        self._evict_expired(current)
        last = self._entries.get(key)
        if last is not None and current - last < self._ttl:
            return True
        self._entries[key] = current
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        """清理已过期条目。"""
        expired = [key for key, ts in self._entries.items() if now - ts >= self._ttl]
        for key in expired:
            self._entries.pop(key, None)
```

- [ ] **Step 5: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/ -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add packages/yuxi-im-gateway
git commit -m "feat(gateway): 新增 IM 网关包骨架与入站过滤纯逻辑"
```

---

### Task 2: Yuxi HTTP 客户端与运行事件归并

**Files:**
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/events.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/yuxi_client.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/config.py`
- Create: `packages/yuxi-im-gateway/tests/test_events.py`
- Create: `packages/yuxi-im-gateway/tests/test_yuxi_client.py`

**Interfaces:**
- Produces:
  - `RunOutcome`（dataclass：`status: str`（`completed`/`approval_required`/`error`）、`text: str`、`artifact_paths: list[str]`、`error_message: str | None`）
  - `reduce_run_events(events: Iterable[dict], thread_id: str | None = None) -> RunOutcome`（`events` 为 `{"event": str, "data": str}`）
  - `iter_sse_events(lines: Iterable[str]) -> Iterator[dict[str, str]]`（移植 CLI 解析）
  - `GatewaySettings`（dataclass，从环境变量构造：`api_base, gateway_token, poll_interval`）
  - `YuxiClient(settings)`：`fetch_bot_configs() -> list[BotConfig]`、`submit_message(bot, thread_id, chat_id, sender_id, message_id, text, request_id) -> dict`、`stream_run(stream_url, api_key) -> Iterator[dict]`、`download_artifact(thread_id, path, api_key) -> bytes`

- [ ] **Step 1: 写失败测试（事件归并）**

```python
# packages/yuxi-im-gateway/tests/test_events.py
import json

from yuxi_im_gateway.events import reduce_run_events


def _ev(event: str, payload: dict) -> dict:
    return {"event": event, "data": json.dumps({"thread_id": "t1", "payload": payload})}


def test_text_deltas_are_concatenated():
    events = [
        _ev("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "hello "}}]}),
        _ev("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "world"}}]}),
        _ev("end", {"status": "completed"}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "completed"
    assert outcome.text == "hello world"


def test_artifacts_collected_from_agent_state():
    events = [
        _ev("custom", {"name": "yuxi.agent_state", "agent_state": {"artifacts": ["/home/gem/user-data/outputs/a.xlsx"]}}),
        _ev("end", {"status": "completed"}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.artifact_paths == ["/home/gem/user-data/outputs/a.xlsx"]


def test_human_approval_interrupt_detected():
    events = [
        _ev("messages", {"items": [{"status": "human_approval_required"}]}),
        _ev("interrupt", {"reason": "human_approval", "chunk": {"status": "human_approval_required"}}),
        _ev("end", {"status": "interrupted", "chunk": {"status": "human_approval_required"}}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "approval_required"


def test_non_approval_interrupt_is_error():
    events = [
        _ev("interrupt", {"reason": "ask_user_question_required", "chunk": {"status": "ask_user_question_required"}}),
        _ev("end", {"status": "interrupted"}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "error"


def test_error_event_surfaces_message():
    events = [_ev("error", {"chunk": {"error_message": "boom", "retryable": False}})]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "error"
    assert outcome.error_message == "boom"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_events.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 events**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/events.py
"""把 Yuxi Agent Run 的 SSE 事件归并为一次运行的结果。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Iterator


@dataclass(slots=True)
class RunOutcome:
    """一次 Agent 运行的归并结果。"""

    status: str  # completed / approval_required / error
    text: str = ""
    artifact_paths: list[str] = field(default_factory=list)
    error_message: str | None = None


def iter_sse_events(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """解析 SSE 文本行，忽略 heartbeat，保留 event / data / id。"""
    event: dict[str, str] = {}
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                event["data"] = "\n".join(data_lines)
                yield event
            event = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field in {"event", "id"}:
            event[field] = value
    if data_lines:
        event["data"] = "\n".join(data_lines)
        yield event


def _chunks(payload: dict) -> list[dict]:
    """取出事件 payload 中的 chunk 列表（items 优先）。"""
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    chunk = payload.get("chunk")
    return [chunk] if isinstance(chunk, dict) else []


def reduce_run_events(events: Iterable[dict], thread_id: str | None = None) -> RunOutcome:
    """把 SSE 事件流归并为正文、交付物路径与终态。"""
    text_parts: list[str] = []
    artifacts: list[str] = []
    approval = False
    text = ""

    for event in events:
        try:
            data = json.loads(event.get("data") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if thread_id and data.get("thread_id") not in {None, thread_id}:
            continue

        event_type = event.get("event") or "message"
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}

        for chunk in _chunks(payload):
            if chunk.get("status") == "human_approval_required":
                approval = True
            stream_event = chunk.get("stream_event")
            if isinstance(stream_event, dict) and stream_event.get("type") == "message_delta":
                content = stream_event.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
            state = chunk.get("agent_state")
            if isinstance(state, dict) and isinstance(state.get("artifacts"), list):
                artifacts.extend(str(path) for path in state["artifacts"])

        if isinstance(payload.get("agent_state"), dict):
            state = payload["agent_state"]
            if isinstance(state.get("artifacts"), list):
                artifacts.extend(str(path) for path in state["artifacts"])

        if event_type == "error":
            chunk = payload.get("chunk") if isinstance(payload.get("chunk"), dict) else {}
            if chunk.get("retryable") is True or payload.get("retryable") is True:
                continue
            message = chunk.get("error_message") or chunk.get("message") or "运行失败"
            return RunOutcome(status="error", text="".join(text_parts), error_message=str(message))

        if event_type == "end":
            text = "".join(text_parts)
            status = str(payload.get("status") or "completed")
            if status == "interrupted":
                return RunOutcome(
                    status="approval_required" if approval else "error",
                    text=text,
                    artifact_paths=list(dict.fromkeys(artifacts)),
                    error_message=None if approval else "运行被中断",
                )
            if status != "completed":
                return RunOutcome(status="error", text=text, error_message=f"运行结束：{status}")
            return RunOutcome(status="completed", text=text, artifact_paths=list(dict.fromkeys(artifacts)))

    return RunOutcome(
        status="approval_required" if approval else "error",
        text="".join(text_parts),
        artifact_paths=list(dict.fromkeys(artifacts)),
        error_message=None if approval else "运行事件流在终态前断开",
    )
```

- [ ] **Step 4: 写失败测试（客户端，使用 respx mock）**

```python
# packages/yuxi-im-gateway/tests/test_yuxi_client.py
import httpx
import pytest
import respx

from yuxi_im_gateway.config import GatewaySettings
from yuxi_im_gateway.yuxi_client import YuxiClient


def _settings() -> GatewaySettings:
    return GatewaySettings(api_base="http://yuxi", gateway_token="tok", poll_interval=30)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_bot_configs_sends_token_header():
    route = respx.get("http://yuxi/api/internal/im-gateway/config").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [{"id": 1, "platform": "feishu", "api_key": "yxkey_x", "agent_slug": "a"}]})
    )
    client = YuxiClient(_settings())
    bots = await client.fetch_bot_configs()
    assert route.calls.last.request.headers["X-IM-Gateway-Token"] == "tok"
    assert bots[0].id == 1


@pytest.mark.asyncio
@respx.mock
async def test_submit_message_uses_bot_api_key():
    route = respx.post("http://yuxi/api/agent-invocation/channel/messages").mock(
        return_value=httpx.Response(200, json={"kind": "run", "run_id": "r1", "stream_url": "/api/agent/runs/r1/events"})
    )
    client = YuxiClient(_settings())
    from yuxi_im_gateway.models import BotConfig

    bot = BotConfig(1, "feishu", "b", {}, "yxkey_z", "a")
    result = await client.submit_message(
        bot=bot, thread_id=None, chat_id="c1", sender_id="ou_a",
        message_id="m1", text="hi", request_id="m1",
    )
    assert result["run_id"] == "r1"
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == "Bearer yyxkey_z".replace("yy", "y")
    body = route.calls.last.request.content
    assert b'"channel":"feishu"' in body
```

- [ ] **Step 5: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_yuxi_client.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 6: 实现 config 与 yuxi_client**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/config.py
"""网关运行配置，全部来自环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class GatewaySettings:
    """网关设置：Yuxi 地址、服务令牌与轮询间隔。"""

    api_base: str
    gateway_token: str
    poll_interval: float

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        """从环境变量构造配置。"""
        return cls(
            api_base=os.environ.get("YUXI_GATEWAY_API_BASE", "http://api:5050").rstrip("/"),
            gateway_token=os.environ.get("IM_GATEWAY_TOKEN", ""),
            poll_interval=float(os.environ.get("IM_GATEWAY_POLL_INTERVAL", "30")),
        )
```

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/yuxi_client.py
"""与 Yuxi HTTP API 的交互：拉取配置、提交消息、消费 SSE、下载附件。"""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from yuxi_im_gateway.config import GatewaySettings
from yuxi_im_gateway.events import iter_sse_events
from yuxi_im_gateway.models import BotConfig


class YuxiClient:
    """基于 httpx 的 Yuxi 客户端，业务调用使用机器人 API Key。"""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=settings.api_base, timeout=httpx.Timeout(30.0, read=None))

    async def aclose(self) -> None:
        """关闭底层连接。"""
        await self._client.aclose()

    async def fetch_bot_configs(self) -> list[BotConfig]:
        """拉取启用中的机器人配置（含解密凭证与 API Key）。"""
        response = await self._client.get(
            "/api/internal/im-gateway/config",
            headers={"X-IM-Gateway-Token": self._settings.gateway_token},
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return [BotConfig.from_payload(item) for item in data]

    async def submit_message(
        self,
        *,
        bot: BotConfig,
        thread_id: str | None,
        chat_id: str,
        sender_id: str,
        message_id: str,
        text: str,
        request_id: str,
    ) -> dict:
        """向 Channel 入口提交一条文本消息或 slash command。"""
        payload = {
            "channel": bot.platform,
            "account_id": str(bot.id),
            "chat_id": chat_id,
            "thread_id": thread_id,
            "sender_id": sender_id,
            "message_id": message_id,
            "request_id": request_id,
            "agent_slug": bot.agent_slug,
            "message": {"type": "text", "text": text},
        }
        response = await self._client.post(
            "/api/agent-invocation/channel/messages",
            json=payload,
            headers={"Authorization": f"Bearer {bot.api_key}"},
        )
        response.raise_for_status()
        return response.json()

    async def stream_run(self, stream_url: str, api_key: str) -> AsyncIterator[list[dict]]:
        """连接 Run SSE，逐批产出解析后的事件（已按 SSE 帧切分）。"""
        path = stream_url[4:] if stream_url.startswith("/api/") else stream_url
        headers = {"Authorization": f"Bearer {api_key}"}
        async with self._client.stream("GET", path, headers=headers, params={"verbose": "false"}) as response:
            response.raise_for_status()
            frame: dict[str, str] = {}
            async for line in response.aiter_lines():
                if not line:
                    if frame:
                        yield [frame]
                    frame = {}
                    continue
                if line.startswith(":"):
                    continue
                field, sep, value = line.partition(":")
                value = value[1:] if sep and value.startswith(" ") else (value if sep else "")
                if field == "data":
                    frame["data"] = f"{frame.get('data', '')}\n{value}" if "data" in frame else value
                elif field in {"event", "id"}:
                    frame[field] = value
            if frame:
                yield [frame]

    async def download_artifact(self, thread_id: str, path: str, api_key: str) -> bytes:
        """下载线程交付物原始字节。"""
        virtual = path.lstrip("/")
        response = await self._client.get(
            f"/api/chat/thread/{thread_id}/artifacts/{virtual}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return response.content
```

- [ ] **Step 7: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_events.py tests/test_yuxi_client.py -v`
Expected: PASS

注意：`test_yuxi_client.py` 中的 `assert auth == "Bearer yyxkey_z".replace("yy", "y")` 写法笨拙，实现时直接断言 `"Bearer yxkey_z"` 即可。

- [ ] **Step 8: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/events.py packages/yuxi-im-gateway/src/yuxi_im_gateway/yuxi_client.py packages/yuxi-im-gateway/src/yuxi_im_gateway/config.py packages/yuxi-im-gateway/tests/test_events.py packages/yuxi-im-gateway/tests/test_yuxi_client.py
git commit -m "feat(gateway): 新增 Yuxi 客户端与运行事件归并"
```

---

### Task 3: 会话串行与运行编排

**Files:**
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py`
- Create: `packages/yuxi-im-gateway/tests/test_runner.py`

**Interfaces:**
- Consumes: `YuxiClient`（Task 2）、`RunOutcome` / `reduce_run_events`（Task 2）、`should_handle` / `is_allowed` / `session_chat_id`（Task 1）。
- Produces:
  - `SessionQueue`：`async run(key, coro_factory)`（同 key 串行）
  - `approval_request_id(decision: str, run_id: str) -> str`
  - `handle_inbound(client, adapter, bot, dedupe, sessions, msg) -> None`
  - `resolve_approval(client, adapter, bot, thread_id, run_id, decision, operator_id, requester_id) -> None`

- [ ] **Step 1: 写失败测试**

```python
# packages/yuxi-im-gateway/tests/test_runner.py
import asyncio

import pytest

from yuxi_im_gateway.runner import SessionQueue, approval_request_id


def test_approval_request_id_is_deterministic():
    assert approval_request_id("approve", "run-1") == approval_request_id("approve", "run-1")
    assert approval_request_id("approve", "run-1") != approval_request_id("reject", "run-1")


@pytest.mark.asyncio
async def test_session_queue_serializes_same_key():
    queue = SessionQueue()
    order: list[str] = []

    async def job(tag: str, delay: float) -> None:
        order.append(f"start-{tag}")
        await asyncio.sleep(delay)
        order.append(f"end-{tag}")

    await asyncio.gather(
        queue.run("k", lambda: job("a", 0.05)),
        queue.run("k", lambda: job("b", 0.0)),
    )
    assert order == ["start-a", "end-a", "start-b", "end-b"]


@pytest.mark.asyncio
async def test_session_queue_runs_different_keys_concurrently():
    queue = SessionQueue()
    order: list[str] = []

    async def job(tag: str, delay: float) -> None:
        order.append(f"start-{tag}")
        await asyncio.sleep(delay)
        order.append(f"end-{tag}")

    await asyncio.gather(
        queue.run("k1", lambda: job("a", 0.05)),
        queue.run("k2", lambda: job("b", 0.0)),
    )
    assert order.index("start-b") < order.index("end-a")
```

- [ ] **Step 2: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_runner.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 runner**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py
"""入站处理与运行编排：去重、白名单、提交、消费、出站、审批。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.events import reduce_run_events
from yuxi_im_gateway.filters import is_allowed, session_chat_id, should_handle
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)


class SessionQueue:
    """保证同一会话键上的任务串行执行，不同会话键并行。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run(self, key: str, factory: Callable[[], Awaitable[None]]) -> None:
        """在 key 对应的锁内执行工厂返回的协程。"""
        async with self._locks[key]:
            await factory()


def approval_request_id(decision: str, run_id: str) -> str:
    """按 decision 与父 run_id 生成确定性 request_id，用于幂等去重。"""
    return f"{decision}:{run_id}"


async def handle_inbound(
    *,
    client,
    adapter,
    bot: BotConfig,
    dedupe: DedupeCache,
    sessions: SessionQueue,
    msg: InboundMessage,
) -> None:
    """处理一条归一化入站消息：过滤 → 去重 → 提交 → 消费 → 出站。"""
    if not should_handle(bot, msg) or not is_allowed(bot, msg.sender_id):
        return
    if dedupe.seen(f"{bot.id}:{msg.message_id}"):
        return

    chat_id = session_chat_id(bot, msg)

    async def job() -> None:
        await _run_and_reply(client=client, adapter=adapter, bot=bot, chat_id=chat_id, msg=msg)

    await sessions.run(chat_id, job)


async def _run_and_reply(*, client, adapter, bot: BotConfig, chat_id: str, msg: InboundMessage) -> None:
    """提交运行、消费事件并把结果或审批卡片发回平台。"""
    try:
        submitted = await client.submit_message(
            bot=bot,
            thread_id=None,
            chat_id=chat_id,
            sender_id=msg.sender_id,
            message_id=msg.message_id,
            text=msg.text,
            request_id=msg.message_id,
        )
    except Exception as exc:  # noqa: BLE001 - 单个会话失败不应中断网关
        logger.warning("提交消息失败 bot=%s: %s", bot.id, exc)
        await adapter.send_text(chat_id, f"调用失败：{exc}")
        return

    stream_url = _resolve_stream_url(client, submitted)
    if not stream_url:
        await adapter.send_text(chat_id, "运行未启动，请稍后重试")
        return

    outcome = await _consume(client, stream_url, bot.api_key, submitted.get("thread_id"))
    if outcome.status == "approval_required":
        await adapter.send_approval_card(
            chat_id,
            thread_id=str(submitted.get("thread_id") or ""),
            run_id=str(submitted.get("run_id") or ""),
            requester_id=msg.sender_id,
        )
        return
    if outcome.status == "error":
        await adapter.send_text(chat_id, f"运行失败：{outcome.error_message}")
        return

    if outcome.text.strip():
        await adapter.send_markdown(chat_id, outcome.text)
    for path in outcome.artifact_paths:
        await _send_artifact(client, adapter, bot, chat_id, str(submitted.get("thread_id") or ""), path)


def _resolve_stream_url(client, submitted: dict) -> str | None:
    """从提交响应解析实际可订阅的 run stream_url。"""
    return submitted.get("stream_url")


async def _consume(client, stream_url: str, api_key: str, thread_id: str | None):
    """订阅 SSE 并归并结果。"""
    collected: list[dict] = []
    async for batch in client.stream_run(stream_url, api_key):
        collected.extend(batch)
    return reduce_run_events(collected, thread_id=thread_id)


async def _send_artifact(client, adapter, bot: BotConfig, chat_id: str, thread_id: str, path: str) -> None:
    """下载交付物并作为附件发送，失败只记录日志。"""
    try:
        content = await client.download_artifact(thread_id, path, bot.api_key)
        filename = path.rstrip("/").split("/")[-1] or "artifact"
        await adapter.send_file(chat_id, filename, content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("发送交付物失败 bot=%s path=%s: %s", bot.id, path, exc)
        await adapter.send_text(chat_id, f"交付物 {path} 发送失败")


async def resolve_approval(
    *,
    client,
    adapter,
    bot: BotConfig,
    thread_id: str,
    run_id: str,
    decision: str,
    operator_id: str,
    requester_id: str,
) -> None:
    """处理卡片审批回调：仅原发送者有效，提交 decision 并回发最终结果。"""
    if operator_id != requester_id:
        return
    if decision not in {"approve", "reject"}:
        return

    request_id = approval_request_id(decision, run_id)
    try:
        result = await client.submit_message(
            bot=bot,
            thread_id=thread_id,
            chat_id="",
            sender_id=operator_id,
            message_id=request_id,
            text=f"/{decision}",
            request_id=request_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("提交审批失败 bot=%s run=%s: %s", bot.id, run_id, exc)
        await adapter.send_text(thread_id, f"审批提交失败：{exc}")
        return

    run = result.get("run") if isinstance(result.get("run"), dict) else result
    stream_url = run.get("stream_url") if isinstance(run, dict) else None
    if not stream_url:
        return
    outcome = await _consume(client, stream_url, bot.api_key, thread_id)
    if outcome.status == "completed" and outcome.text.strip():
        await adapter.send_markdown(thread_id, outcome.text)
    elif outcome.status == "error":
        await adapter.send_text(thread_id, f"运行失败：{outcome.error_message}")
```

注意：`submit_message` 在审批场景需要允许 `chat_id=""`（后端会依据显式 `thread_id` 解析），实现时确认 Channel 入口在 `thread_id` 非空时不因空 `chat_id` 报错（`_resolve_thread_id` 优先用 `thread_id`）。

- [ ] **Step 4: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_runner.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/runner.py packages/yuxi-im-gateway/tests/test_runner.py
git commit -m "feat(gateway): 新增会话串行与运行编排"
```

---

### Task 4: Adapter 抽象与注册表

**Files:**
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/__init__.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/base.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/registry.py`
- Create: `packages/yuxi-im-gateway/tests/test_adapter_registry.py`

**Interfaces:**
- Produces:
  - `Adapter` 抽象基类：`async start() -> None`、`async stop() -> None`、`async send_text(chat_id, text)`、`async send_markdown(chat_id, text)`、`async send_file(chat_id, filename, content: bytes)`、`async send_approval_card(chat_id, *, thread_id, run_id, requester_id)`、属性 `fingerprint -> str`（用于判断配置是否变化）。
  - `build_adapter(bot, on_inbound) -> Adapter`（按 `bot.platform` 分发）
  - `adapter_fingerprint(bot) -> str`（platform + 凭证值 + agent_slug 的稳定拼接，供热更新比对）
  - `on_inbound` 回调签名：`Callable[[InboundMessage], Awaitable[None]]`

- [ ] **Step 1: 写失败测试**

```python
# packages/yuxi-im-gateway/tests/test_adapter_registry.py
import pytest

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.adapters.registry import adapter_fingerprint, build_adapter
from yuxi_im_gateway.models import BotConfig


def _bot(platform: str, creds: dict) -> BotConfig:
    return BotConfig(1, platform, "b", creds, "k", "a")


def test_fingerprint_changes_with_credentials():
    assert adapter_fingerprint(_bot("feishu", {"app_id": "1"})) != adapter_fingerprint(
        _bot("feishu", {"app_id": "2"})
    )


def test_build_adapter_rejects_unknown_platform():
    with pytest.raises(ValueError):
        build_adapter(_bot("slack", {}), on_inbound=lambda m: None)


def test_build_adapter_returns_adapter_for_feishu():
    adapter = build_adapter(_bot("feishu", {"app_id": "1", "app_secret": "s"}), on_inbound=lambda m: None)
    assert isinstance(adapter, Adapter)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_adapter_registry.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 base 与 registry**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/base.py
"""平台适配器抽象：网关只依赖该接口，不依赖具体平台 SDK。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Adapter(ABC):
    """一个机器人对应的平台连接与出站能力。"""

    @abstractmethod
    async def start(self) -> None:
        """建立长连接并开始接收事件。"""

    @abstractmethod
    async def stop(self) -> None:
        """关闭连接，释放资源。"""

    @abstractmethod
    async def send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息。"""

    @abstractmethod
    async def send_markdown(self, chat_id: str, text: str) -> None:
        """发送 markdown 富文本消息，失败时降级为纯文本。"""

    @abstractmethod
    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        """发送文件附件。"""

    @abstractmethod
    async def send_approval_card(self, chat_id: str, *, thread_id: str, run_id: str, requester_id: str) -> None:
        """发送工具审批交互卡片。"""
```

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/registry.py
"""按平台构建适配器，并提供配置指纹用于热更新比对。"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.models import BotConfig, InboundMessage

OnInbound = Callable[[InboundMessage], Awaitable[None]]


def adapter_fingerprint(bot: BotConfig) -> str:
    """生成能反映连接关键配置的指纹，用于判断是否需要重建连接。"""
    return json.dumps(
        {
            "platform": bot.platform,
            "credentials": bot.credentials,
            "agent_slug": bot.agent_slug,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def build_adapter(bot: BotConfig, *, on_inbound: OnInbound) -> Adapter:
    """按平台构建适配器实例。"""
    if bot.platform == "feishu":
        from yuxi_im_gateway.adapters.feishu import FeishuAdapter

        return FeishuAdapter(bot, on_inbound=on_inbound)
    if bot.platform == "dingtalk":
        from yuxi_im_gateway.adapters.dingtalk import DingTalkAdapter

        return DingTalkAdapter(bot, on_inbound=on_inbound)
    raise ValueError(f"不支持的平台：{bot.platform}")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_adapter_registry.py -v`
Expected: PASS（此步会因 feishu/dingtalk 模块尚不存在而在 feishu 用例失败——先实现 Task 5/6 的两个 adapter 后再跑完整注册表测试。）

- [ ] **Step 5: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/__init__.py packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/base.py packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/registry.py packages/yuxi-im-gateway/tests/test_adapter_registry.py
git commit -m "feat(gateway): 新增适配器抽象与注册表"
```

---

### Task 5: 飞书适配器

**Files:**
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/feishu.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/cards.py`
- Create: `packages/yuxi-im-gateway/tests/test_cards.py`

**Interfaces:**
- Consumes: `Adapter`（Task 4）。
- Produces:
  - `cards.py`：`approval_card(thread_id, run_id, requester_id) -> dict`、`parse_card_action(value: dict) -> tuple[str, str, str, str]`（返回 `decision, thread_id, run_id, requester_id`）
  - `FeishuAdapter(bot, *, on_inbound)`，`start` 用 `lark-oapi` WS 客户端在后台线程运行并把事件调度回主 loop。

- [ ] **Step 1: 写失败测试（卡片纯逻辑）**

```python
# packages/yuxi-im-gateway/tests/test_cards.py
from yuxi_im_gateway.adapters.cards import approval_card, parse_card_action


def test_parse_card_action_round_trip():
    card = approval_card(thread_id="t1", run_id="r1", requester_id="ou_a")
    approve_button = card["actions"][0]
    decision, thread_id, run_id, requester_id = parse_card_action(approve_button["value"])
    assert (decision, thread_id, run_id, requester_id) == ("approve", "t1", "r1", "ou_a")


def test_parse_card_action_tolerates_string_value():
    decision, thread_id, run_id, requester_id = parse_card_action(
        '{"decision": "reject", "thread_id": "t", "run_id": "r", "requester_id": "o"}'
    )
    assert decision == "reject"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_cards.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 cards**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/cards.py
"""审批交互卡片的构造与回调解析（平台无关的中间结构）。"""

from __future__ import annotations

import json


def approval_card(thread_id: str, run_id: str, requester_id: str) -> dict:
    """构造含批准 / 拒绝两个按钮的审批卡片描述。"""
    base = {"thread_id": thread_id, "run_id": run_id, "requester_id": requester_id}
    return {
        "title": "工具审批",
        "text": "Agent 请求执行一个需要审批的工具操作，请选择：",
        "actions": [
            {"label": "批准", "decision": "approve", "value": {**base, "decision": "approve"}},
            {"label": "拒绝", "decision": "reject", "value": {**base, "decision": "reject"}},
        ],
    }


def parse_card_action(value) -> tuple[str, str, str, str]:
    """解析按钮回调 value，返回 (decision, thread_id, run_id, requester_id)。"""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("卡片回调 value 格式无效")
    return (
        str(value.get("decision") or ""),
        str(value.get("thread_id") or ""),
        str(value.get("run_id") or ""),
        str(value.get("requester_id") or ""),
    )
```

- [ ] **Step 4: 实现 FeishuAdapter**

按 `lark-oapi` 官方版本实现，关键结构如下（具体方法名以安装版本为准）：

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/feishu.py
"""飞书适配器：基于 lark-oapi WebSocket 长连接收发消息与卡片。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.adapters.cards import approval_card, parse_card_action
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)

OnInbound = Callable[[InboundMessage], Awaitable[None]]


class FeishuAdapter(Adapter):
    """单个飞书机器人的长连接适配器。"""

    def __init__(self, bot: BotConfig, *, on_inbound: OnInbound) -> None:
        self._bot = bot
        self._on_inbound = on_inbound
        self._loop = asyncio.get_event_loop()
        self._thread = None

    async def start(self) -> None:
        """在后台线程启动 lark-oapi WS 客户端。"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        domain = self._bot.credentials.get("domain") or "feishu"
        lark_domain = lark.LARK_DOMAIN if domain == "lark" else lark.FEISHU_DOMAIN

        def on_message(data: P2ImMessageReceiveV1) -> None:
            message = self._normalize(data)
            if message is not None:
                asyncio.run_coroutine_threadsafe(self._on_inbound(message), self._loop)

        def on_card(data) -> None:
            self._handle_card(data)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_card_action_trigger(on_card)
            .build()
        )
        ws_client = lark.ws.Client(
            self._bot.credentials["app_id"],
            self._bot.credentials["app_secret"],
            event_handler=handler,
            domain=lark_domain,
        )
        self._thread = _run_in_thread(ws_client.start)

    async def stop(self) -> None:
        """停止后台线程（由进程退出兜底）。"""
        self._thread = None

    def _normalize(self, data) -> InboundMessage | None:
        """把飞书消息事件归一化为 InboundMessage。"""
        event = data.event
        message = event.message
        message_type = getattr(message, "message_type", "")
        if message_type != "text":
            self._loop.create_task(self.send_text(message.chat_id, "暂不支持非文本消息"))
            return None
        content = json.loads(message.content or "{}")
        text = str(content.get("text") or "")
        chat_type = getattr(message, "chat_type", "p2p")
        is_group = chat_type == "group"
        mentions = getattr(message, "mentions", None) or []
        mentions_bot = bool(mentions) if is_group else False
        sender_id = event.sender.sender_id.open_id
        return InboundMessage(
            message_id=message.message_id,
            chat_id=message.chat_id,
            sender_id=sender_id,
            is_group=is_group,
            mentions_bot=mentions_bot,
            text=text.strip(),
        )

    def _handle_card(self, data) -> None:
        """处理卡片回调：解析 value 并调度审批。"""
        value = getattr(getattr(data, "event", None), "action", None)
        value = getattr(value, "value", None)
        operator_id = str(getattr(getattr(data.event, "operator", None), "open_id", ""))
        try:
            decision, thread_id, run_id, requester_id = parse_card_action(value)
        except (ValueError, json.JSONDecodeError):
            return
        from yuxi_im_gateway.runner import resolve_approval

        asyncio.run_coroutine_threadsafe(
            self._resolve(decision, thread_id, run_id, operator_id, requester_id),
            self._loop,
        )

    async def _resolve(self, decision, thread_id, run_id, operator_id, requester_id) -> None:
        """从主 loop 调用编排层处理审批（client/bot 由 main 注入）。"""
        callback = getattr(self, "approval_handler", None)
        if callback is not None:
            await callback(
                decision=decision, thread_id=thread_id, run_id=run_id,
                operator_id=operator_id, requester_id=requester_id,
            )

    async def send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息。"""
        self._send_message(chat_id, "text", json.dumps({"text": text}, ensure_ascii=False))

    async def send_markdown(self, chat_id: str, text: str) -> None:
        """用 post 消息内嵌 md 标签发送，失败降级为纯文本。"""
        content = json.dumps({"zh_cn": {"content": [[{"tag": "md", "text": text}]]}}, ensure_ascii=False)
        try:
            self._send_message(chat_id, "post", content)
        except Exception:  # noqa: BLE001
            await self.send_text(chat_id, text)

    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        """上传并发送文件。"""
        raise NotImplementedError

    async def send_approval_card(self, chat_id: str, *, thread_id: str, run_id: str, requester_id: str) -> None:
        """发送审批交互卡片。"""
        self._send_message(chat_id, "interactive", json.dumps(_feishu_card(approval_card(thread_id, run_id, requester_id)), ensure_ascii=False))

    def _send_message(self, chat_id: str, msg_type: str, content: str) -> None:
        """用 lark OpenAPI 主动发送消息（同步调用）。"""
        raise NotImplementedError
```

同时实现 `_run_in_thread(target)`（`threading.Thread(target=target, daemon=True).start()`）与 `_feishu_card(card)`（把平台无关卡片转成飞书 `interactive` schema 2.0，按钮 `value` 直接放 `card["actions"][i]["value"]`）。`send_file` 与 `_send_message` 补完：lark `im.v1.file.create` 上传拿 `file_key`，再发 `msg_type="file"`；发送用 `lark.Client.builder().app_id(...).app_secret(...).build().im.v1.message.create(...)`。

在 `main.py` 装配时给 adapter 注入 `approval_handler` 回调（见 Task 7）。

- [ ] **Step 5: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_cards.py -v`
Expected: PASS（`send_*` 的 SDK 路径靠手工联调验证）

- [ ] **Step 6: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/feishu.py packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/cards.py packages/yuxi-im-gateway/tests/test_cards.py
git commit -m "feat(gateway): 新增飞书适配器与审批卡片"
```

---

### Task 6: 钉钉适配器

**Files:**
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/dingtalk.py`
- Modify: `packages/yuxi-im-gateway/tests/test_adapter_registry.py`（启用钉钉注册用例）

**Interfaces:**
- Consumes: `Adapter`（Task 4）、`approval_card` / `parse_card_action`（Task 5）。
- Produces: `DingTalkAdapter(bot, *, on_inbound)`，用 `dingtalk-stream` 的 Stream Mode 接收消息，出站优先 `robotCode` 主动发送、session webhook 回退。

- [ ] **Step 1: 写失败测试（注册表含钉钉）**

在 `tests/test_adapter_registry.py` 追加：

```python
def test_build_adapter_returns_adapter_for_dingtalk():
    adapter = build_adapter(
        _bot("dingtalk", {"client_id": "1", "client_secret": "s"}), on_inbound=lambda m: None
    )
    assert isinstance(adapter, Adapter)
```

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_adapter_registry.py -v`
Expected: FAIL（`ImportError: dingtalk` 模块缺失）

- [ ] **Step 2: 实现 DingTalkAdapter**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/dingtalk.py
"""钉钉适配器：基于 dingtalk-stream Stream Mode 收发消息。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Awaitable, Callable

import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)

OnInbound = Callable[[InboundMessage], Awaitable[None]]


class DingTalkAdapter(Adapter):
    """单个钉钉机器人的 Stream Mode 适配器。"""

    def __init__(self, bot: BotConfig, *, on_inbound: OnInbound) -> None:
        self._bot = bot
        self._on_inbound = on_inbound
        self._loop = asyncio.get_event_loop()
        self._webhooks: dict[str, str] = {}
        self._client: dingtalk_stream.DingTalkStreamClient | None = None

    async def start(self) -> None:
        """后台线程运行 Stream 客户端。"""
        credential = dingtalk_stream.Credential(
            self._bot.credentials["client_id"], self._bot.credentials["client_secret"]
        )
        self._client = dingtalk_stream.DingTalkStreamClient(credential)
        handler = _ChatbotHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)
        threading.Thread(target=self._client.start_forever, daemon=True).start()

    async def stop(self) -> None:
        """停止长连接（进程退出兜底）。"""
        self._client = None

    def _normalize(self, message: ChatbotMessage) -> InboundMessage:
        """把钉钉回调消息归一化为 InboundMessage。"""
        is_group = bool(message.conversation_type == "2")
        text = str((message.text and message.text.content) or "").strip()
        return InboundMessage(
            message_id=str(message.message_id),
            chat_id=str(message.conversation_id),
            sender_id=str(message.sender_staff_id),
            is_group=is_group,
            mentions_bot=bool(message.is_in_at_list) if is_group else False,
            text=_strip_at(text, message),
        )

    async def send_text(self, chat_id: str, text: str) -> None:
        """优先用 robotCode 主动发送，session webhook 回退。"""
        await self._send(chat_id, {"msgtype": "text", "text": {"content": text}})

    async def send_markdown(self, chat_id: str, text: str) -> None:
        """发送 markdown 消息，失败降级为纯文本。"""
        try:
            await self._send(chat_id, {"msgtype": "markdown", "markdown": {"title": "结果", "text": text}})
        except Exception:  # noqa: BLE001
            await self.send_text(chat_id, text)

    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        """上传临时素材并发送文件消息。"""
        raise NotImplementedError

    async def send_approval_card(self, chat_id: str, *, thread_id: str, run_id: str, requester_id: str) -> None:
        """发送 actionCard 审批卡片。"""
        card = approval_card(thread_id, run_id, requester_id)
        buttons = [
            {"title": action["label"], "actionURL": "", "value": json.dumps(action["value"])}
            for action in card["actions"]
        ]
        body = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": card["title"],
                "text": card["text"],
                "btnOrientation": "0",
                "btns": buttons,
            },
        }
        await self._send(chat_id, body)

    async def _send(self, chat_id: str, body: dict) -> None:
        """通过 robotCode 主动发送，失败回退 session webhook。"""
        raise NotImplementedError


def _strip_at(text: str, message: ChatbotMessage) -> str:
    """去掉钉钉消息前缀中的 @机器人 文本。"""
    at_bot = None
    is_at_bot = False
    if isinstance(message.at_users, list):
        for user in message.at_users:
            if getattr(user, "is_bot", False):
                is_at_bot = True
                at_bot = getattr(user, "dingtalk_id", None)
    if at_bot:
        return text.replace(f"@{at_bot}", "").strip()
    return text.strip()


class _ChatbotHandler(ChatbotHandler):
    """把 dingtalk-stream 回调桥接到 asyncio 主循环。"""

    def __init__(self, adapter: DingTalkAdapter) -> None:
        super().__init__()
        self._adapter = adapter

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """处理入站消息并返回 ACK。"""
        message = ChatbotMessage.from_dict(callback.data)
        adapter = self._adapter
        if message.conversation_type == "2":
            adapter._webhooks[message.conversation_id] = callback.data.get("sessionWebhook", "")
        inbound = adapter._normalize(message)
        if inbound.text:
            asyncio.run_coroutine_threadsafe(adapter._on_inbound(inbound), adapter._loop)
        return AckMessage.STATUS_OK, "OK"
```

补齐 `_send`：优先用钉钉 OpenAPI（`topapi/message/corpconversation/asyncsend_v2` 或机器人 `robotCode` 主动发送），失败则用 `self._webhooks.get(chat_id)` POST 回退；卡片按钮回调由钉钉在 webhook 回调中携带 `value`，在 `process` 中检测 `callback.data.get("type") == "actionCallback"` 时改走审批逻辑并注入 `approval_handler`（与飞书一致）。

- [ ] **Step 3: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_adapter_registry.py -v`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/adapters/dingtalk.py packages/yuxi-im-gateway/tests/test_adapter_registry.py
git commit -m "feat(gateway): 新增钉钉适配器"
```

---

### Task 7: 主程序与配置轮询热更新

**Files:**
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/main.py`
- Create: `packages/yuxi-im-gateway/src/yuxi_im_gateway/__main__.py`
- Create: `packages/yuxi-im-gateway/tests/test_main.py`

**Interfaces:**
- Consumes: 全部前序模块。
- Produces: `GatewayRunner(client, settings)`（`reconcile(configs)` 增删改连、`run()` 主循环）、`async main() -> None`、`def main() -> None`（同步入口）。

- [ ] **Step 1: 写失败测试（reconcile 纯逻辑，用 fake adapter）**

```python
# packages/yuxi-im-gateway/tests/test_main.py
import pytest

from yuxi_im_gateway.adapters.registry import adapter_fingerprint
from yuxi_im_gateway.main import GatewayRunner
from yuxi_im_gateway.models import BotConfig


class _FakeAdapter:
    instances: list["_FakeAdapter"] = []

    def __init__(self, bot, *, on_inbound):
        self.bot = bot
        self.started = False
        self.stopped = False
        _FakeAdapter.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def _bot(bot_id: int, app_id: str) -> BotConfig:
    return BotConfig(bot_id, "feishu", "b", {"app_id": app_id, "app_secret": "s"}, "k", "a")


@pytest.fixture(autouse=True)
def _reset():
    _FakeAdapter.instances.clear()
    yield
    _FakeAdapter.instances.clear()


@pytest.mark.asyncio
async def test_reconcile_starts_new_bots(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    assert _FakeAdapter.instances[0].started is True


@pytest.mark.asyncio
async def test_reconcile_rebuilds_on_credential_change(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([_bot(1, "b")])
    assert _FakeAdapter.instances[0].stopped is True
    assert _FakeAdapter.instances[1].started is True


@pytest.mark.asyncio
async def test_reconcile_keeps_unchanged_bot(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([_bot(1, "a")])
    assert len(_FakeAdapter.instances) == 1


@pytest.mark.asyncio
async def test_reconcile_stops_removed_bot(monkeypatch):
    import yuxi_im_gateway.main as mod

    monkeypatch.setattr(mod, "build_adapter", lambda bot, on_inbound: _FakeAdapter(bot, on_inbound=on_inbound))
    runner = GatewayRunner(client=None, settings=None)
    await runner.reconcile([_bot(1, "a")])
    await runner.reconcile([])
    assert _FakeAdapter.instances[0].stopped is True
```

- [ ] **Step 2: 运行确认失败**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/test_main.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 main**

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/main.py
"""网关主程序：加载机器人、维护连接、周期性同步配置。"""

from __future__ import annotations

import asyncio
import logging
import signal

from yuxi_im_gateway.adapters.registry import adapter_fingerprint, build_adapter
from yuxi_im_gateway.config import GatewaySettings
from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.models import BotConfig, InboundMessage
from yuxi_im_gateway.runner import SessionQueue, handle_inbound, resolve_approval
from yuxi_im_gateway.yuxi_client import YuxiClient

logger = logging.getLogger(__name__)


class GatewayRunner:
    """持有全部机器人适配器，负责配置同步与热更新。"""

    def __init__(self, client: YuxiClient, settings: GatewaySettings) -> None:
        self._client = client
        self._settings = settings
        self._adapters: dict[int, tuple[str, object]] = {}
        self._bots: dict[int, BotConfig] = {}
        self._dedupe = DedupeCache()
        self._sessions = SessionQueue()
        self._stopping = asyncio.Event()

    async def reconcile(self, configs: list[BotConfig]) -> None:
        """按最新配置增删改机器人连接，未变化的保持不动。"""
        desired = {bot.id: bot for bot in configs}
        for bot_id in list(self._adapters.keys()):
            bot = desired.get(bot_id)
            current_fingerprint = self._adapters[bot_id][0]
            if bot is None or adapter_fingerprint(bot) != current_fingerprint:
                await self._stop_bot(bot_id)
        for bot_id, bot in desired.items():
            if bot_id not in self._adapters:
                await self._start_bot(bot)
        self._bots = desired

    async def _start_bot(self, bot: BotConfig) -> None:
        """启动单个机器人适配器，失败只记录日志。"""
        try:
            adapter = build_adapter(bot, on_inbound=lambda msg, b=bot: self._on_inbound(b, msg))
            self._attach_approval(adapter, bot)
            await adapter.start()
            self._adapters[bot.id] = (adapter_fingerprint(bot), adapter)
            logger.info("机器人已启动 id=%s platform=%s", bot.id, bot.platform)
        except Exception as exc:  # noqa: BLE001 - 故障隔离
            logger.error("机器人启动失败 id=%s: %s", bot.id, exc)

    async def _stop_bot(self, bot_id: int) -> None:
        """停止并移除单个机器人适配器。"""
        entry = self._adapters.pop(bot_id, None)
        if entry is None:
            return
        try:
            await entry[1].stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("机器人停止失败 id=%s: %s", bot_id, exc)

    def _attach_approval(self, adapter, bot: BotConfig) -> None:
        """给适配器注入审批回调。"""

        async def on_approval(*, decision, thread_id, run_id, operator_id, requester_id):
            await resolve_approval(
                client=self._client, adapter=adapter, bot=bot, thread_id=thread_id,
                run_id=run_id, decision=decision, operator_id=operator_id, requester_id=requester_id,
            )

        adapter.approval_handler = on_approval

    async def _on_inbound(self, bot: BotConfig, msg: InboundMessage) -> None:
        """入站消息入口，交给编排层。"""
        await handle_inbound(
            client=self._client, adapter=self._adapters[bot.id][1], bot=bot,
            dedupe=self._dedupe, sessions=self._sessions, msg=msg,
        )

    async def run(self) -> None:
        """主循环：启动后周期拉取配置并同步。"""
        while not self._stopping.is_set():
            try:
                configs = await self._client.fetch_bot_configs()
                await self.reconcile(configs)
            except Exception as exc:  # noqa: BLE001
                logger.error("同步机器人配置失败: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._settings.poll_interval)
            except asyncio.TimeoutError:
                continue

    def request_stop(self) -> None:
        """请求优雅退出。"""
        self._stopping.set()


async def main() -> None:
    """阻塞运行网关。"""
    logging.basicConfig(level=logging.INFO)
    settings = GatewaySettings.from_env()
    if not settings.gateway_token:
        raise SystemExit("IM_GATEWAY_TOKEN 未配置")
    client = YuxiClient(settings)
    runner = GatewayRunner(client, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, runner.request_stop)
    try:
        await runner.run()
    finally:
        await client.aclose()


def main() -> None:
    """同步入口（脚本与 `python -m`）。"""
    asyncio.run(main)


if __name__ == "__main__":
    main()
```

```python
# packages/yuxi-im-gateway/src/yuxi_im_gateway/__main__.py
from yuxi_im_gateway.main import main

main()
```

- [ ] **Step 4: 运行确认通过**

Run: `cd packages/yuxi-im-gateway && uv run --group test pytest tests/ -v`
Expected: PASS（全部单测）

- [ ] **Step 5: 提交**

```bash
git add packages/yuxi-im-gateway/src/yuxi_im_gateway/main.py packages/yuxi-im-gateway/src/yuxi_im_gateway/__main__.py packages/yuxi-im-gateway/tests/test_main.py
git commit -m "feat(gateway): 新增主程序与配置轮询热更新"
```

---

### Task 8: Docker 集成、环境变量与变更日志

**Files:**
- Create: `docker/gateway.Dockerfile`
- Modify: `docker-compose.yml`（新增 `gateway` 服务）
- Modify: `docker-compose.prod.yml`（新增 `gateway` 服务）
- Modify: `.env.template`（新增网关环境变量示例）
- Modify: `docs/develop-guides/changelog.md`

- [ ] **Step 1: 新增 Dockerfile**

```dockerfile
# docker/gateway.Dockerfile
FROM python:3.13-slim

RUN pip install uv --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

ENV TZ=Asia/Shanghai \
    UV_PROJECT_ENVIRONMENT="/usr/local" \
    UV_COMPILE_BYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

RUN set -ex \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY packages/yuxi-im-gateway/pyproject.toml /app/pyproject.toml
COPY packages/yuxi-im-gateway/uv.lock /app/uv.lock
COPY packages/yuxi-im-gateway/src /app/src

RUN uv sync --no-cache --no-dev --frozen
```

生成 `packages/yuxi-im-gateway/uv.lock`：`cd packages/yuxi-im-gateway && uv lock`。

- [ ] **Step 2: compose 新增 gateway 服务**

在 `docker-compose.yml` 的 `worker` 服务之后加入：

```yaml
  gateway:
    build:
      context: .
      dockerfile: docker/gateway.Dockerfile
    image: yuxi-gateway:${YUXI_VERSION:-0.7.1}
    container_name: gateway-dev
    working_dir: /app
    volumes:
      - ./packages/yuxi-im-gateway/src:/app/src
      - ./packages/yuxi-im-gateway/pyproject.toml:/app/pyproject.toml
    ports: []
    networks:
      - app-network
    extra_hosts:
      - "host.docker.internal:host-gateway"
    env_file:
      - .env
    environment:
      - YUXI_GATEWAY_API_BASE=${YUXI_GATEWAY_API_BASE:-http://api:5050}
      - IM_GATEWAY_TOKEN=${IM_GATEWAY_TOKEN:-}
      - IM_GATEWAY_POLL_INTERVAL=${IM_GATEWAY_POLL_INTERVAL:-30}
    command: watchfiles --filter python "python -m yuxi_im_gateway" /app/src
    restart: unless-stopped
    depends_on:
      api:
        condition: service_healthy
```

`docker-compose.prod.yml` 同步加入 `gateway`（去掉热重载 volumes 与 `watchfiles`，`command: python -m yuxi_im_gateway`），并可选择 `profiles: ["gateway"]` 让默认部署不含网关（按部署需要启用）。

- [ ] **Step 3: .env.template 补充**

```
# region IM 网关（可选，启用飞书 / 钉钉机器人时配置）
# 与 api 服务共享：未配置时 /api/internal/im-gateway/* 返回 404
IM_GATEWAY_TOKEN=
# gateway 访问 Yuxi 的地址（compose 内默认 http://api:5050）
# YUXI_GATEWAY_API_BASE=http://api:5050
# 配置同步轮询间隔（秒）
# IM_GATEWAY_POLL_INTERVAL=30
# endregion IM 网关
```

- [ ] **Step 4: 启动并手工联调**

Run: `docker compose up -d gateway`，然后 `docker logs gateway-dev --tail 50`
Expected: 日志无异常；配置了机器人后出现「机器人已启动」。

手工验收（对照 spec 验收标准逐项）：
1. 飞书单聊文本问答，markdown 正确渲染。
2. 钉钉单聊文本问答正常。
3. 群聊仅在被 @ 时响应，群内用户会话隔离。
4. Agent 交付 Excel 时作为附件发出。
5. 触发工具审批时收到卡片，批准与拒绝均生效，重复点击不产生第二个恢复 run。
6. `allowed_users` 非空时白名单外用户被忽略；留空时所有人可用。

- [ ] **Step 5: 更新 changelog**

在 `docs/develop-guides/changelog.md` 最新一条之上新增：新增 IM 网关服务 `packages/yuxi-im-gateway`，支持飞书 / 钉钉 WebSocket 长连接接入、markdown 正文与附件回发、交互卡片工具审批、配置热更新；compose 新增 `gateway` 服务与相关环境变量。

- [ ] **Step 6: 提交**

```bash
git add docker/gateway.Dockerfile docker-compose.yml docker-compose.prod.yml .env.template packages/yuxi-im-gateway/uv.lock docs/develop-guides/changelog.md docs/develop-guides/changelog.md
git commit -m "feat(gateway): 接入 docker-compose 与部署配置"
```

---

## Self-Review

- **Spec coverage**：总体结构 / 数据模型（Plan 1）/ 运行时（Task 3/7）/ 入站（Task 1/3）/ 出站（Task 4/5/6，附件在 Task 3）/ 审批（Task 3/5/6）/ 部署（Task 8）均覆盖。非目标（不改 Agent Run、不入站文件、不切 Agent、无 webhook、单账号）在设计中已排除，未引入相关代码。
- **接口一致性**：`BotConfig` / `InboundMessage` 在 Task 1 定义并全程复用；`Adapter` 接口在 Task 4 定义，Task 5/6 实现，Task 7 装配；`approval_handler` 由 Task 5/6 定义、Task 7 注入；`resolve_approval` / `handle_inbound` 在 Task 3 定义、Task 7 调用。
- **Review Focus 对应**：纯逻辑可测（Task 1/2/3/5 单测）、审批幂等（`approval_request_id` 测试 + Task 8 手工项）、串行与去重（SessionQueue / DedupeCache 测试）、故障隔离（`_start_bot` / `_stop_bot` try/except）、出站降级（`send_markdown` 降级）、凭证不落盘（无持久化）。
- **已知待联调项**：飞书 / 钉钉 SDK 的具体调用（`_send_message`、`send_file`、Stream 回调结构）依赖安装版本，必须用真实应用完成 Task 8 的手工联调；这些在计划中已显式标注 `NotImplementedError` 与「以安装版本为准」。
