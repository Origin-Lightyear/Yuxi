# Agent 输入框支持语音录入

状态：implemented
类型：feature
Owner：web/src/components/AgentInputArea.vue

跨 Owner 分工：`web/src/components/AgentInputArea.vue` 拥有入口按钮、草稿判定与发送落点；`web/src/composables/useSpeechInput.js` 拥有录音、静音判定、客户端重采样与转写请求；`web/src/utils/speech_input.js` 拥有可用性、状态推进与结果落点等纯函数；`backend/package/yuxi/services/speech_service.py` 拥有 ASR 调用、超时与失败分类；`backend/server/routers/chat_router.py` 拥有音频入口校验；`backend/server/routers/system_router.py` 拥有能力探测与缓存；`docker/asr/` 拥有识别服务与模型加载；`backend/package/yuxi/config/options.py` 拥有 `asr_host_opts`。

## 问题

Agent 会话输入框只接受文字与文件。用户腾不出手时（手上在做别的事、边走边说）没有可用的输入路径，只能放弃提问或延后。仓库在此之前没有任何录音或语音识别链路：对 `backend/` 与 `web/src/` 搜索 `SpeechRecognition`、`MediaRecorder`、`getUserMedia`、`whisper`、`asr`、`语音`、`录音` 均无命中。

场景约束来自实际使用：短句（一句问题，通常 5–20 秒）、中文为主、说完立即发送、不需要看到句中的实时中间结果。这类输入的正确体验参照 Hermes 语音模式：按一次起录、静音自动结束、转写完成直接作为消息发出，不插入确认步骤。

## 决策

### 实现方案

新增一个自托管 ASR 服务，由 API 中继调用，前端只负责采集与回填。

**识别服务**：`docker/asr.Dockerfile` 构建独立镜像，镜像内安装 FunASR 与 `iic/SenseVoiceSmall`（构建期下载到 `/opt/model`，运行期不联网）。服务是薄 FastAPI 应用，暴露 `POST /transcribe`（接收 16 kHz 单声道 16-bit WAV，返回 `{"text": ...}`）与 `GET /health`。`/health` 只在模型加载完成后返回 200，Compose healthcheck 用它区分「容器起了」与「能转写」。推理在进程内用互斥锁串行执行，单进程 CPU；请求超过 30 秒由 API 侧判超时。

该服务位于 `voice` profile，`docker compose up` 与 CI 的默认拓扑不启动它。启用方式为 `docker compose --profile voice up`，并在环境文件设置 `ASR_API_URI=http://asr:8100`；未配置时前端按不可用处理。模型协议要求的署名落在镜像内的协议全文与模型名称，部署文档的组件表列出该依赖。

**配置**：`yuxi/config/options.py` 的 `asr_host_opts` 提供 `server_url` 字段，读取环境变量 `ASR_API_URI`，可在系统设置页修改，未配置时解析为空串。

**后端**：`speech_service` 调用识别服务，按 `rich_transcription_postprocess` 语义剥离 SenseVoice 富文本标签（`<|zh|><|NEUTRAL|><|Speech|><|withitn|>` 一类），并定义 30 秒转写超时、3 秒健康探测与结构化失败（不可用、超时、识别为空）。`POST /api/chat/speech/transcribe` 只做认证、`audio/wav` 校验、≤60 秒与 ≤5 MB 上限检查，然后调用 service；路由读取有界原始请求体，避免 `UploadFile` 把大请求体落到临时文件。

音频全程在内存中流转：不写 PostgreSQL、不进 MinIO、不落本地磁盘，转写完成后丢弃。日志只记录字节数与耗时，不记录音频内容或识别文本。

**能力发现**：`GET /api/system/speech/health` 面向登录用户，探测结果缓存 30 秒；前端进入会话页时探测一次。`/api/system/ready` 不纳入 ASR：ASR 是可选的输入增强，缺失时文字输入与会话主链路保持可用。

**前端**：`AgentInputArea.vue` 在 `actions-right` 插入麦克风按钮（`@lucide/vue` 的 `Mic`/`Square`/`Loader`），与发送键相邻。录音逻辑在 `useSpeechInput.js`：

- `getUserMedia` 采单声道，开启 `echoCancellation`、`noiseSuppression`、`autoGainControl`
- `MediaRecorder` 取原始音频；停止后经 `AudioContext.decodeAudioData` 与 `OfflineAudioContext` 重采样为 16 kHz 单声道 WAV Blob，服务端不做转码
- 边录边算 RMS：检测到语音后累计静音 800 ms 自动停止；起录后 5 秒内未检测到语音则取消并提示「没听到声音」；60 秒硬上限
- 转写成功后先回填 `modelValue`，`nextTick` 后触发发送，保证输入框内容与发出的消息一致；转写中再次点击取消
- 权限被拒、服务不可用、转写为空都不发送空消息，输入框原有文字保持不变，并给出可读提示

按钮三态（录音中、转写中、不可用）由探测结果与本地录音状态共同决定；服务不可用或输入框禁用时按钮隐藏，不渲染成可点击的失败入口。

**交互边界**

- 输入框已有草稿或附件时拒绝起录并提示先处理；追加或替换会让用户无法判断哪部分是自己写的。
- 会话处于等待用户操作或发送被禁用时，允许录音与转写，但只回填输入框、不自动发送，把是否现在发送交还给该状态的持有者。
- 录音开始时记录 thread ID；会话切换、离开页面或组件卸载会取消录音并丢弃结果，避免把 A 会话的转写发到 B 会话。
- 转写返回时输入框仍为空才回填并自动发送；用户在转写期间输入了内容时，转写结果追加在已有文字之后且不自动发送。转写期间不锁输入框，返回结果不覆盖用户刚输入的文字。

**已知取舍**

默认模型为 SenseVoiceSmall 而非 Paraformer：它是非自回归结构，短音频延迟更低，中英混说（`Agent`、`Docker`、`知识库`）在其训练覆盖范围内。代价是不支持热词表。若术语错误率影响使用，把识别服务内的模型换成 `paraformer-zh` + `fsmn-vad` + `ct-punc` 并接热词表，`/transcribe` 契约不变，前端与后端不需要改动；切换条件写在验证项里，不预先实现两套。

## 替代方案

- **浏览器 `SpeechRecognition`**：零部署、零成本，Chrome 走 Google 语音服务。国内网络基本不可用，无法配置热词，也无法控制音频去向，作为产品功能不可接受。
- **云厂商流式 ASR**（讯飞语音听写、阿里 `qwen-audio-3.1-asr-flash-message`、火山豆包流式）：中文准确率最高，但有每日额度与外部依赖。把语音链路绑定到厂商账号、额度与网络出口，不适合作为长期产品前提。
- **自托管 Whisper（faster-whisper / whisper.cpp）**：免费离线，中文是明显弱项（短句 CER 8–12%），输入还会被补齐到 30 秒帧长，短语音浪费算力。
- **Meta Omnilingual ASR**：Apache-2.0、语种多，但只接受 40 秒以内整段音频，输出无标点无大小写，目标是长尾语言而非中文准确率。
- **流式 FunASR（Paraformer 2-pass）**：能边说边出字，需要同时跑实时与离线两路模型，内存与实现复杂度翻倍；本期按整段识别交付，留作后续演进。
- **把音频当普通附件上传，交给支持音频模态的模型**：现有附件入口已允许 ≤5 MB 任意格式，技术上可行，但无法控制识别质量，也不能自动发送，不满足「说完就发」。

## 后果

- 识别镜像约 2.7 GB（CPU 版 torch），构建期需要联网下载模型。启用 `voice` profile 的部署要付出一次构建与导出成本，离线部署需先构建并 `docker save` 该镜像。
- 识别容器以 root 运行，不挂载宿主目录、只读取镜像内模型；该取舍用于避免为非 root 用户额外维护模型与端口权限。
- CPU 推理在单进程内串行执行，多人同时使用时请求排队并可能触发 30 秒超时；首版按「先可用、再扩容」处理，并发预算需要上线后观测。
- SenseVoiceSmall 的权重按 FunASR Model Open Source License v1.1 分发，第 2.2 节要求保留署名与模型名称。镜像内附协议全文（`docker/asr/MODEL_LICENSE.txt`）与署名（`docker/asr/NOTICE.txt`），部署文档同步说明该依赖。
- `getUserMedia` 要求安全上下文。生产入口的 HTTPS 由宿主机反向代理提供；桌面端 Electron 壳默认连接开发机的 HTTP 地址，该地址不是安全上下文，语音入口在那里不可用，需要指向 HTTPS 地址。
- iOS Safari 对 `MediaRecorder` 的支持随版本变化，旧版本隐藏入口；该边界只能按真实设备验证。
- 静音阈值使用固定 RMS 值，不同麦克风与噪声环境表现不一致；首版按实测调整一次，不做自适应校准。
- 不提供流式反馈，说完到看见文字之间存在等待；超过 2 秒需要重新评估模型选择。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 中文短句可转写并自动发出 | 录音成功但消息未发出，或发出的文本与输入框不一致 | `web/src/composables/useSpeechInput.js` | 系统 TTS 生成 1.95 秒中文（「知识库里有什么内容」）经 `POST /api/chat/speech/transcribe` 转写为「知识库里有什么内容？」，端到端 0.97 秒；浏览器中静音自动结束并自动发出已有分支实测记录（Chrome + 假麦克风） | 停掉 `asr` 容器后重试，消息列表不新增空消息 | Passed |
| 静音自动结束，不产生空消息 | 一直录不停，或无声也发送 | `useSpeechInput.js` 的 RMS 判定 | `web/test/unit/speech_input.test.js` 锁定 4999/5000 ms 边界；浏览器中静音 800 ms 后自动结束 | 起录后静默 10 秒，断言未调用 `transcribe` | Passed |
| 音频不被持久化 | 音频或识别文本进入数据库、对象存储或日志 | `speech_service.py`、`chat_router.py` | 转写前后 `messages` 446→446、api 容器 `/tmp` 条目 5→5，代码无数据库、MinIO、磁盘写入；日志只记录字节数与耗时 | 构造一次失败请求，确认失败路径同样无残留 | Passed |
| 超限输入被拒绝 | 超长音频拖垮服务或被静默截断 | `chat_router.py` 的入口校验 | `backend/test/integration/api/test_speech_router.py` 覆盖非 WAV / 6 MB / 61 秒；真实 API 复核：无鉴权 401、非 WAV 400、61 秒 400、6 MB 413 | 逐项构造越界请求断言状态码 | Passed |
| ASR 缺失时可观察降级 | 麦克风按钮可点但永远失败，或文字输入被一起拖坏 | `/api/system/speech/health` + `AgentInputArea.vue` | 后端事实：停掉 `asr` 容器后 `/api/system/speech/health` 返回 `available: false`，转写返回 503，`/api/system/ready` 仍为 ready；按钮显隐逻辑由 `web/test/unit/speech_input.test.js` 覆盖 | 停掉 `asr` 后刷新页面，按钮消失、文字输入正常（页面路径未复测） | Inspected |
| 权限拒绝不损坏输入框 | 录音失败清空已输入内容 | `useSpeechInput.js` 的错误分支 | `getUserMedia` 失败时只提示，不触碰 `modelValue` | 拒绝权限后连续点击两次麦克风，不出现未捕获异常 | Inspected |
| 非安全上下文不提供无效入口 | 用 HTTP 访问时按钮可点、点了必然失败 | `useSpeechInput.js` 的可用性判定 | `isSpeechAvailable` 单测覆盖 `secureContext=false` 返回不可用；`http://localhost` 下按钮渲染；非 localhost 的 HTTP 入口未在浏览器验证 | 在 HTTP 入口断言按钮不存在，且控制台无未捕获异常（未复测） | Inspected |
| 识别镜像可从仓库 Dockerfile 构建并离线运行 | 构建失败或运行期依赖网络 | `docker/asr.Dockerfile` | `docker compose --profile voice build asr` 成功；镜像约 2.7 GB，`torch 2.14.1+cpu`、无 CUDA；`docker run --network none` 后 `/health` 返回 200 | 断网容器仍可加载模型并返回 200 | Passed |
| 中文识别质量达到可用 | 术语错到无法阅读，用户需要整句重打 | 识别服务内的模型选择 | 需要团队自录 20 条模拟提问（不使用生产会话历史），覆盖 `Agent`、`知识库`、`图谱` 等术语，逐条记字错率与是否需整句重打；判据为平均字错率 ≤8% 且整句重打比例 ≤10% | 未达判据时按「已知取舍」切 Paraformer-zh + 热词后重测同样 20 条 | Not run |
