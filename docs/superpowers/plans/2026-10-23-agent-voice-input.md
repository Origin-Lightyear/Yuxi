# Agent 输入框语音录入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent 会话输入框支持「按一下 → 说一句中文 → 静音自动结束 → 转写并自动发送」，识别完全在本地自托管，不依赖任何外部厂商。

**Architecture:** 新增一个独立的 FunASR 识别容器（`voice` profile，默认不启动），API 作为中继调用它，浏览器只负责采集、重采样与回填。音频在内存中走完即弃，不落库、不落盘。前端把可判定的规则拆成纯函数单测，浏览器能力（录音、重采样）留在 composable 里。

**Tech Stack:** FastAPI + httpx（后端）、FunASR 1.x + SenseVoiceSmall（识别服务，CPU）、Vue 3 + MediaRecorder + WebAudio（前端）、node:test（前端单测）、pytest（后端单测/集成）。

**Spec:** `docs/develop-guides/decisions/proposed/2026-10-23-agent-voice-input.md`

## Global Constraints

- 识别容器放在 `voice` profile：`docker compose up` 与 CI 默认拓扑不启动它。
- 音频只走内存：不写 PostgreSQL、不进 MinIO、不落本地磁盘；路由读有界原始 body，不用 `UploadFile`。
- 入口限制：`audio/wav`、单声道 16 kHz、≤60 秒、≤5 MB。
- 转写结果只在输入框为空时回填并自动发送；否则追加到已有文字之后、不发送。
- 录音开始时锁定 thread ID；会话切换、离开页面、组件卸载一律取消并丢弃结果。
- 非安全上下文（`http://` 且非 localhost）不渲染麦克风按钮。
- 本次不新增 TLS 终止，不改 Web 容器的 80 端口；Electron 默认地址不动。
- 不通过放宽证书校验、`--unsafely-treat-insecure-origin-as-secure` 等手段让 HTTP 源可用。
- 新增函数/类写简洁中文 docstring；不记录音频内容或识别文本到日志。
- 提交信息用中文 Conventional Commit。

## Review Focus

- **AI 会话等待用户操作或发送被禁用时说话**：用户预期文字留在输入框里、不会替他发出去。`handleSendMessage` 已在这两个状态下静默返回，前端只回填不重试。
- **背景噪声大、或用户说完立刻改口**：800 ms 静音判定可能提前截断或永不停。60 秒硬上限必须兜住，且不能产生空消息。
- **转写期间用户开始打字**：预期两份内容都在、不会互相覆盖。追加策略在不发送的同时保留用户输入。
- **转写过程中切换会话或关闭页面**：预期旧会话不产生消息。thread ID 锁定 + 卸载取消要覆盖这个窗口。
- **识别服务未启动或超时**：预期按钮不可用、文字输入照旧。不能卡在「转写中」或弹出无限 loading。

---

### Task 1: 识别服务地址配置项

**Files:**
- Modify: `backend/package/yuxi/config/options.py`（在 `paddleocr_api_opts` 之后新增，并加入 `OPTION_DEFINITIONS`）
- Test: `backend/test/unit/config/test_options.py`

**Interfaces:**
- Produces: `asr_host_opts: Option`，`await asr_host_opts.get(db)` 返回 `{"server_url": str}`；未配置时 `server_url` 为空字符串。

- [ ] **Step 1: 写失败测试**

在 `backend/test/unit/config/test_options.py` 追加：

```python
def test_asr_host_opts_registered_with_env_fallback():
    from yuxi.config.options import OPTION_DEFINITIONS, asr_host_opts

    assert "asr_host_opts" in OPTION_DEFINITIONS
    field = asr_host_opts.params["fields"][0]
    assert field["key"] == "server_url"
    assert field["environment"] == "ASR_API_URI"
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/unit/config/test_options.py::test_asr_host_opts_registered_with_env_fallback -v`
Expected: FAIL，`ImportError: cannot import name 'asr_host_opts'`

- [ ] **Step 3: 实现配置项**

在 `paddleocr_api_opts` 之后定义，`key="asr_host_opts"`、`name="语音识别服务"`、字段 `server_url`（`type="url"`、`environment="ASR_API_URI"`、`placeholder="http://asr:8100"`、help 说明「留空时语音输入不可用」），并把 `asr_host_opts` 加进 `OPTION_DEFINITIONS` 的元组。

- [ ] **Step 4: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/unit/config/test_options.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/config/options.py backend/test/unit/config/test_options.py
git commit -m "feat(config): 新增语音识别服务地址配置项"
```

---

### Task 2: 自托管识别容器

**Files:**
- Create: `docker/asr/app.py`
- Create: `docker/asr.Dockerfile`
- Modify: `docker-compose.yml`（新增 `asr` 服务）
- Modify: `.env.template`（新增注释掉的 `ASR_API_URI`）

**Interfaces:**
- Produces: `POST http://asr:8100/transcribe`，请求体为 WAV 字节，响应 `{"text": str}`；音轨无效返回 400，模型未就绪返回 503。
- Produces: `GET http://asr:8100/health`，模型加载完成返回 200，否则 503。

- [ ] **Step 1: 写服务**

`docker/asr/app.py`：FastAPI 应用，模块导入时用 `AutoModel(model="iic/SenseVoiceSmall", device="cpu", disable_update=True)` 加载一次模型并保存在模块级变量；`generate(input=wav_bytes, language="zh", use_itn=True)` 后取 `[0]["text"]`，用 `rich_transcription_postprocess` 去标签。`/health` 检查模块级模型变量是否为 None。启动时若模型缺失直接退出，不进入假就绪状态。

- [ ] **Step 2: 写镜像**

`docker/asr.Dockerfile` 基于 `python:3.13-slim`，装 `funasr`、`torch`（CPU wheel）、`fastapi`、`uvicorn`、`soundfile`，构建期执行一次 `AutoModel(...)` 把模型下载到 `MODELSCOPE_CACHE=/opt/modelscope`，之后 `ENV MODELSCOPE_CACHE=/opt/modelscope` 且 `HF_HUB_OFFLINE=1`，运行期不联网。`CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]`。

- [ ] **Step 3: 接入 Compose**

`docker-compose.yml` 新增 `asr` 服务：`profiles: ["voice"]`、镜像名 `${COMPOSE_PROJECT_NAME:-yuxi}-asr:${YUXI_VERSION:-0.7.3}`、端口 `127.0.0.1:${YUXI_ASR_PORT:-8100}:8100`、healthcheck 用 `python -c "import urllib.request;urllib.request.urlopen('http://localhost:8100/health')"`、`interval: 20s`、`start_period: 120s`、`networks: [app-network]`、`restart: unless-stopped`。`.env.template` 增加注释：启用语音输入需 `docker compose --profile voice up` 并设置 `ASR_API_URI=http://asr:8100`。

- [ ] **Step 4: 验证容器真的能转写**

Run:
```bash
docker compose --profile voice up -d --build asr
docker compose exec api python -c "import urllib.request,json;print(urllib.request.urlopen('http://asr:8100/health').status)"
```
Expected: `200`；`docker compose ps asr` 显示 healthy。

再用一段真实中文 WAV（16 kHz 单声道）验证：

```bash
curl -s -X POST --data-binary @sample.wav -H 'Content-Type: audio/wav' http://localhost:8100/transcribe
```
Expected: 返回 `{"text": "..."}`，文本无 `<|zh|>` 一类标签。

- [ ] **Step 5: 提交**

```bash
git add docker/asr/app.py docker/asr.Dockerfile docker-compose.yml .env.template
git commit -m "feat(asr): 新增自托管 FunASR 识别服务"
```

---

### Task 3: 后端转写服务

**Files:**
- Create: `backend/package/yuxi/services/speech_service.py`
- Test: `backend/test/unit/services/test_speech_service.py`

**Interfaces:**
- Produces: `strip_rich_tags(text: str) -> str`
- Produces: `class SpeechError(RuntimeError)`，属性 `reason: str`（`"unavailable"` / `"timeout"` / `"empty"`）
- Produces: `async def transcribe_speech(audio: bytes, *, server_url: str | None = None, db: AsyncSession | None = None) -> str`，失败抛 `SpeechError`
- Produces: `async def check_speech_health(db: AsyncSession | None = None) -> bool`
- Produces: `def build_asr_client(server_url: str) -> httpx.AsyncClient`（测试通过替换它注入 `MockTransport`）

- [ ] **Step 1: 写失败测试**

`backend/test/unit/services/test_speech_service.py`：

```python
import httpx
import pytest

from yuxi.services import speech_service
from yuxi.services.speech_service import SpeechError, strip_rich_tags, transcribe_speech


def test_strip_rich_tags_removes_sensevoice_tags():
    assert strip_rich_tags("<|zh|><|NEUTRAL|><|Speech|><|withitn|>今天几号？") == "今天几号？"


def test_strip_rich_tags_keeps_plain_text():
    assert strip_rich_tags("今天几号？") == "今天几号？"


@pytest.mark.asyncio
async def test_transcribe_speech_returns_stripped_text(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "audio/wav"
        return httpx.Response(200, json={"text": "<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好"})

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    assert await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100") == "你好"


@pytest.mark.asyncio
async def test_transcribe_speech_maps_timeout(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    with pytest.raises(SpeechError) as exc:
        await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100")
    assert exc.value.reason == "timeout"


@pytest.mark.asyncio
async def test_transcribe_speech_maps_unavailable(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    with pytest.raises(SpeechError) as exc:
        await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100")
    assert exc.value.reason == "unavailable"


@pytest.mark.asyncio
async def test_transcribe_speech_maps_empty_result(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "<|zh|><|NEUTRAL|><|Speech|><|withitn|>"})

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    with pytest.raises(SpeechError) as exc:
        await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100")
    assert exc.value.reason == "empty"
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_speech_service.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'yuxi.services.speech_service'`

- [ ] **Step 3: 实现服务**

`transcribe_speech(audio, *, server_url=None, db=None)`：`server_url` 为 None 时读 `asr_host_opts.get(db)`；为空则抛 `reason="unavailable"`。超时 30 秒，`httpx.TimeoutException` → `"timeout"`，非 200 → `"unavailable"`，`text` 去标签后为空 → `"empty"`。`check_speech_health` 对 `/health` 发 GET，超时 3 秒，任何异常返回 False。模块级常量 `ASR_TIMEOUT_SECONDS = 30`、`ASR_HEALTH_TIMEOUT_SECONDS = 3`。日志只记时长与字节数。

- [ ] **Step 4: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_speech_service.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/services/speech_service.py backend/test/unit/services/test_speech_service.py
git commit -m "feat(speech): 新增语音转写服务与失败分类"
```

---

### Task 4: 转写路由与能力探测

**Files:**
- Modify: `backend/server/routers/chat_router.py`（新增转写端点与响应模型）
- Modify: `backend/server/routers/system_router.py`（新增探测端点）
- Test: `backend/test/integration/api/test_speech_router.py`

**Interfaces:**
- Produces: `class SpeechTranscribeResponse(BaseModel)`，字段 `text: str`
- Produces: `POST /api/chat/speech/transcribe`，`Content-Type: audio/wav`，成功 200 `{"text": "..."}`；`SpeechError` 映射为 503（unavailable）/ 504（timeout）/ 422（empty）；请求本身不合法（类型、大小、WAV 头）由路由直接返回 400 / 413
- Produces: `GET /api/system/speech/health`，返回 `{"available": bool}`，结果缓存 30 秒

- [ ] **Step 1: 写失败测试**

`backend/test/integration/api/test_speech_router.py`：

```python
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

WAV_HEADER = b"RIFF$\x00\x00\x00WAVEfmt "


async def test_transcribe_rejects_non_wav(test_client, admin_headers):
    response = await test_client.post(
        "/api/chat/speech/transcribe",
        content=b"not audio",
        headers={**admin_headers, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 400


async def test_transcribe_rejects_oversized_body(test_client, admin_headers):
    response = await test_client.post(
        "/api/chat/speech/transcribe",
        content=WAV_HEADER + b"\x00" * (6 * 1024 * 1024),
        headers={**admin_headers, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 413


async def test_transcribe_requires_auth(test_client):
    response = await test_client.post(
        "/api/chat/speech/transcribe",
        content=WAV_HEADER,
        headers={"Content-Type": "audio/wav"},
    )
    assert response.status_code == 401


async def test_speech_health_reports_availability(test_client, admin_headers):
    response = await test_client.get("/api/system/speech/health", headers=admin_headers)
    assert response.status_code == 200
    assert isinstance(response.json()["available"], bool)
```

成功转写需要真实中文音频与运行中的识别服务，仓库内没有可复用的音频夹具，因此这一条不写成自动化用例：由 Task 2 的 curl 验证和 Task 6 的真实页面验证覆盖，并在决策记录的验收表里如实标注未进 CI。

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec api uv run --group test pytest test/integration/api/test_speech_router.py -v -m integration`
Expected: FAIL，404（端点不存在）

- [ ] **Step 3: 实现端点**

`chat_router.py`：`chat.post("/speech/transcribe", response_model=SpeechTranscribeResponse)`；先校验 `Content-Type` 以 `audio/wav` 开头，再边读 `request.stream()` 边累计，超过 5 MB 立即 413，累计后不足 WAV 头长度或 `RIFF`/`WAVE` 不匹配则 400；`SpeechError.reason` 映射到上表状态码；成功返回 `{"text": text}`。

`system_router.py`：`system.get("/speech/health")`；调用 `check_speech_health(db)`，模块级 30 秒 TTL 缓存，返回 `{"available": ...}`，不抛异常。

- [ ] **Step 4: 运行确认通过**

Run: `docker compose exec api uv run --group test pytest test/integration/api/test_speech_router.py -v -m integration`
Expected: 4 passed

再确认没有残留：请求前后比对临时目录与数据库。

Run:
```bash
docker compose exec api sh -c 'ls -l /tmp | wc -l'
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB -c "select count(*) from messages"
```
Expected: 两条命令在转写请求前后输出一致；`/tmp` 没有新增上传残留文件。

- [ ] **Step 5: 提交**

```bash
git add backend/server/routers/chat_router.py backend/server/routers/system_router.py backend/test/integration/api/test_speech_router.py
git commit -m "feat(speech): 新增转写接口与能力探测"
```

---

### Task 5: 前端判定规则（纯函数）

**Files:**
- Create: `web/src/utils/speech_input.js`
- Test: `web/test/unit/speech_input.test.js`

**Interfaces:**
- Produces: `SPEECH_SILENCE_MS = 800`、`SPEECH_NO_SPEECH_CANCEL_MS = 5000`、`SPEECH_MAX_RECORD_MS = 60000`、`SPEECH_RMS_THRESHOLD = 0.02`
- Produces: `isSpeechAvailable({ secureContext, healthAvailable, disabled }) -> boolean`
- Produces: `nextRecorderState({ elapsedMs, speechDetected, silentMs, rms }) -> 'continue' | 'stop' | 'cancel'`
- Produces: `decideTranscriptAction({ draft, threadIdAtStart, currentThreadId }) -> 'send' | 'append' | 'discard'`

- [ ] **Step 1: 写失败测试**

`web/test/unit/speech_input.test.js`：

```js
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SPEECH_MAX_RECORD_MS,
  decideTranscriptAction,
  isSpeechAvailable,
  nextRecorderState
} from '../../src/utils/speech_input.js'

test('非安全上下文不提供语音入口', () => {
  assert.equal(isSpeechAvailable({ secureContext: false, healthAvailable: true, disabled: false }), false)
  assert.equal(isSpeechAvailable({ secureContext: true, healthAvailable: true, disabled: false }), true)
})

test('识别服务不可用或输入框禁用时不提供入口', () => {
  assert.equal(isSpeechAvailable({ secureContext: true, healthAvailable: false, disabled: false }), false)
  assert.equal(isSpeechAvailable({ secureContext: true, healthAvailable: true, disabled: true }), false)
})

test('检测到语音后累计静音 800ms 停止', () => {
  assert.equal(
    nextRecorderState({ elapsedMs: 3000, speechDetected: true, silentMs: 799, rms: 0.001 }),
    'continue'
  )
  assert.equal(
    nextRecorderState({ elapsedMs: 3000, speechDetected: true, silentMs: 800, rms: 0.001 }),
    'stop'
  )
})

test('起录 5 秒仍无语音则取消', () => {
  assert.equal(
    nextRecorderState({ elapsedMs: 4999, speechDetected: false, silentMs: 0, rms: 0.001 }),
    'continue'
  )
  assert.equal(
    nextRecorderState({ elapsedMs: 5000, speechDetected: false, silentMs: 0, rms: 0.001 }),
    'cancel'
  )
})

test('超过 60 秒强制停止', () => {
  assert.equal(
    nextRecorderState({ elapsedMs: SPEECH_MAX_RECORD_MS, speechDetected: true, silentMs: 0, rms: 0.5 }),
    'stop'
  )
})

test('输入框为空且会话未变才自动发送', () => {
  assert.equal(decideTranscriptAction({ draft: '', threadIdAtStart: 't1', currentThreadId: 't1' }), 'send')
  assert.equal(decideTranscriptAction({ draft: '我打了一半', threadIdAtStart: 't1', currentThreadId: 't1' }), 'append')
  assert.equal(decideTranscriptAction({ draft: '', threadIdAtStart: 't1', currentThreadId: 't2' }), 'discard')
})
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec web node --test test/unit/speech_input.test.js`
Expected: FAIL，`Cannot find module '.../speech_input.js'`

- [ ] **Step 3: 实现纯函数**

判定顺序：会话变了先 `discard`；否则草稿非空 `append`、空草稿 `send`。录音状态：`elapsedMs >= SPEECH_MAX_RECORD_MS` 先判 `stop`；未检测到语音且 `elapsedMs >= SPEECH_NO_SPEECH_CANCEL_MS` 判 `cancel`；检测到语音且 `silentMs >= SPEECH_SILENCE_MS` 判 `stop`；其余 `continue`。`isSpeechAvailable` 三项全真才为真。

- [ ] **Step 4: 运行确认通过**

Run: `docker compose exec web node --test test/unit/speech_input.test.js`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add web/src/utils/speech_input.js web/test/unit/speech_input.test.js
git commit -m "feat(web): 新增语音录入判定规则"
```

---

### Task 6: 录音 composable 与麦克风入口

**Files:**
- Create: `web/src/composables/useSpeechInput.js`
- Create: `web/src/apis/speech_api.js`
- Modify: `web/src/apis/index.js`
- Modify: `web/src/components/AgentInputArea.vue`（`actions-right` 加按钮，接入 composable）

**Interfaces:**
- Consumes: Task 5 的四个常量与 `isSpeechAvailable` / `nextRecorderState` / `decideTranscriptAction`
- Consumes: `POST /api/chat/speech/transcribe`、`GET /api/system/speech/health`（Task 4）
- Produces: `useSpeechInput({ getThreadId, getDraft, onFill }) -> { status, available, start, stop, cancel }`，`status` 取值 `'idle' | 'recording' | 'transcribing'`

- [ ] **Step 1: 写 API 模块**

`web/src/apis/speech_api.js`：

```js
import { apiGet, apiRequest } from './base'

export const speechApi = {
  transcribe: (wavBlob) =>
    apiRequest('/api/chat/speech/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'audio/wav' },
      body: wavBlob
    }),
  health: () => apiGet('/api/system/speech/health')
}
```

`web/src/apis/index.js` 增加 `export * from './speech_api'`。

- [ ] **Step 2: 写 composable**

`useSpeechInput.js`：`getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } })`；`MediaRecorder` 录制；`AnalyserNode` 每 100 ms 取 RMS，喂给 `nextRecorderState`，返回 `stop` 时停录、`cancel` 时释放轨道并提示「没听到声音」；停止后 `decodeAudioData` + `OfflineAudioContext(1, ceil(duration * 16000), 16000)` 重采样，编码为 16-bit PCM WAV Blob；调用 `speechApi.transcribe`。失败按 `SpeechError` 状态码给出可读提示并回到 `idle`；`stop()` 与 `cancel()` 都释放全部轨道与 AudioContext。

- [ ] **Step 3: 接入输入框**

`AgentInputArea.vue` 的 `actions-right` 插槽内、发送按钮左侧渲染麦克风按钮：`Mic`（idle）、`Square`（recording）、`Loader`（transcribing）。点击行为：idle → `start()`；recording → `stop()`；transcribing → `cancel()`。起录前若 `props.modelValue` 非空、`currentImages` 或 `previewAttachments` 非空则提示并拒绝。`watch(() => props.threadId)` 时调 `cancel()`。结果处理：`decideTranscriptAction` 返回 `send` 时先 `emit('update:modelValue', text)` 再 `await nextTick()` 后调用 `handleSend()`；返回 `append` 时只 `emit('update:modelValue', 已有文字 + text)`；返回 `discard` 时丢弃。

`send` 分支不需要新增 prop 或判断 `isWaitingForUserAction`：父组件的 `handleSendMessage` 已在这两个状态下静默返回，文字留在输入框里，正是期望行为。不要为了「确保发送成功」再加一层重试或强制发送。

- [ ] **Step 4: 验证**

```bash
docker compose exec web pnpm run lint:check
docker compose exec web pnpm run test:unit
docker compose exec web pnpm run build
```
Expected: 三条命令均成功。

真实页面验证（必须）：启用 `voice` profile 与 `ASR_API_URI` 后，在 HTTPS 页面录一句中文，确认自动发送；在输入框已有文字时起录，确认被拒绝；切换会话时录音中，确认结果被丢弃；停掉 `asr` 容器后刷新，确认按钮不可用而文字输入正常。

- [ ] **Step 5: 提交**

```bash
git add web/src/composables/useSpeechInput.js web/src/apis/speech_api.js web/src/apis/index.js web/src/components/AgentInputArea.vue
git commit -m "feat(web): 输入框支持语音录入"
```

---

### Task 7: 文档与决策记录收口

**Files:**
- Modify: `docs/advanced/deployment.md`（组件表与语音输入启用步骤）
- Modify: `docs/develop-guides/decisions/proposed/2026-10-23-agent-voice-input.md`（补充实施后的验证结果）
- Modify: `docs/develop-guides/decisions/README.md` 之外的导航不需要改动（决策记录不进用户导航）

- [ ] **Step 1: 写部署说明**

在组件表加入 `asr`，写明：默认不启动、`docker compose --profile voice up`、`ASR_API_URI`、镜像约 2 GB、CPU 推理、模型协议的署名要求；并写明语音入口要求 HTTPS 安全上下文，HTTP 入口不显示该按钮。

- [ ] **Step 2: 记录实施结果**

把 8 条验收主张的「当前结果」列从上到下改写为 `Passed` / `Inspected` / `Not run` 并附上实际命令与输出摘要；未执行的原因写在同格。记录保持 `proposed`，实现完成后由 PR 移到 `implemented/` 并改写为现在时。

- [ ] **Step 3: 跑文档与工程契约检查**

```bash
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
git diff --check
```
Expected: 全部通过；失败则修文档或链接，不放宽检查。

- [ ] **Step 4: 提交**

```bash
git add docs/advanced/deployment.md docs/develop-guides/decisions/proposed/2026-10-23-agent-voice-input.md
git commit -m "docs: 补充语音输入的部署与验证说明"
```

---

## 交付前的整体验证

```bash
docker compose exec api uv run --group test pytest test/unit -m "not slow"
docker compose exec api uv run --group test pytest test/integration/api/test_speech_router.py -m integration
docker compose exec web pnpm run lint:check
docker compose exec web pnpm run test:unit
docker compose exec web pnpm run build
python3 scripts/verify_engineering_contracts.py
git diff --check
```

真实链路（需 `--profile voice`）：录一句中文 → 自动发出 → 消息内容与转写一致；停掉 `asr` → 按钮不可用、文字输入正常。
