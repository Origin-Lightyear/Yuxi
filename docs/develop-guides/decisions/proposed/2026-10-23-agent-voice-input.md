# Agent 输入框支持语音录入

状态：proposed
类型：feature
Owner：web/src/components/AgentInputArea.vue

## 问题

Agent 会话输入框只接受文字与文件。用户腾不出手时（手上在做别的事、边走边说）没有可用的输入路径，只能放弃提问或延后。仓库当前没有任何录音或语音识别链路：对 `backend/` 与 `web/src/` 搜索 `SpeechRecognition`、`MediaRecorder`、`getUserMedia`、`whisper`、`asr`、`语音`、`录音` 均无命中。

场景约束来自实际使用：短句（一句问题，通常 5–20 秒）、中文为主、说完立即发送、不需要看到句中的实时中间结果。这类输入的正确体验参照 Hermes 语音模式——按一次起录、静音自动结束、转写完成直接作为消息发出，不插入确认步骤。

## 提案

### 实现方案

新增一个自托管 ASR 服务，由 API 中继调用，前端只负责采集与回填。

**识别服务**：`docker/asr.Dockerfile` 构建独立镜像，镜像内安装 FunASR 与 `iic/SenseVoiceSmall`（构建期下载到固定缓存路径，运行期不联网）。服务本身是薄 FastAPI 应用，暴露 `POST /transcribe`（接收 16 kHz 单声道 16-bit WAV，返回 `{"text": ...}`）与 `GET /health`。`/health` 只在模型加载完成后返回 200，Compose healthcheck 用它区分「容器起了」与「能转写」。Container 默认用 CPU，单进程，并发上限 2，超出返回 503。

该服务放在 `voice` profile，`docker compose up` 与 CI 的默认拓扑不启动它，也不承担 1.5–2 GB 的拉取与构建成本。启用方式是 `docker compose --profile voice up`，`ASR_API_URI` 缺省时前端按不可用处理。模型协议要求的署名落实在镜像内的协议全文与模型名称，并在部署文档的组件说明里列出该依赖。

**配置**：`yuxi/config/options.py` 新增 `asr_host_opts`，字段 `server_url` 读环境变量 `ASR_API_URI`，与 `mineru_ocr_host_opts` 同构，可在系统设置页修改。

**后端**：`yuxi/services/speech_service.py` 负责调用识别服务、按 `rich_transcription_postprocess` 语义剥离 SenseVoice 富文本标签（`<|zh|><|NEUTRAL|><|Speech|><|withitn|>` 一类），并定义超时（30 秒）与结构化失败（不可用、超时、识别为空）。`server/routers/chat_router.py` 新增 `POST /api/chat/speech/transcribe`，只做认证、`audio/wav` 校验、≤60 秒与 ≤5 MB 上限检查，然后调用 service。路由读取有界原始请求体而不是 `UploadFile`：Starlette 的 `UploadFile` 会把超过 1 MB 的请求体落到临时文件，与下面「不落本地磁盘」的声明冲突。

音频全程在内存中流转：不写 PostgreSQL、不进 MinIO、不落本地磁盘，转写完成后丢弃。日志只记录时长、字节数与耗时，不记录音频内容或识别文本。

**能力发现**：新增 `GET /api/system/speech/health`（登录用户，结果缓存 30 秒），前端进入会话页时探测一次。`/api/system/ready` 不纳入 ASR——ASR 是可选的输入增强，缺失时不影响文字输入与会话主链路。

**前端**：`web/src/components/AgentInputArea.vue` 在 `actions-right` 插入麦克风按钮（`@lucide/vue` 的 `Mic`/`Square`），与发送键相邻，对应 Hermes 把录音键放在输入位旁边的做法。录音逻辑抽到 `web/src/composables/useSpeechInput.js`：

- `getUserMedia` 采单声道，开 `echoCancellation`、`noiseSuppression`、`autoGainControl`
- `MediaRecorder` 取原始音频；停止后经 `AudioContext.decodeAudioData` 与 `OfflineAudioContext` 重采样为 16 kHz 单声道 WAV Blob，服务端不做转码
- 边录边算 RMS：检测到语音后累计静音 800 ms 自动停止；起录后 5 秒内未检测到语音则取消并提示「没听到声音」；60 秒硬上限
- 转写成功后先回填 `modelValue` 再立即触发 `send`，使输入框内容与发出的消息一致；转写中再次点击可取消
- 权限被拒、服务不可用、转写为空三种情况都不发送空消息，输入框原有文字保持不变，并给出可读提示

按钮三态（录音中、转写中、不可用）由探测结果和本地录音状态共同决定；服务不可用时按钮隐藏而不是渲染成可点击的失败入口。

### 交互边界

录音与发送都必须服从输入区已有的状态机，不能绕开它直接调用发送：

- 输入框已有草稿时拒绝起录并提示先处理草稿。追加或替换都会让用户无法判断哪部分是自己写的，丢字比多说一句更糟。
- 当前会话处于等待用户操作或发送被禁用时，允许录音与转写，但只回填输入框、不自动发送，把「要不要现在发」交还给那个状态的持有者。
- 录音开始时记录当时的 thread ID。会话切换、离开页面或组件卸载都会取消录音并丢弃结果，不把 A 会话的转写发到 B 会话。
- 转写返回时输入框仍为空才回填并自动发送；若用户在转写期间已经输入了内容，转写结果追加在已有文字之后且不自动发送。转写期间不锁输入框，也不让返回结果覆盖用户刚敲的字。

### 已知取舍

默认模型选 SenseVoiceSmall 而非 Paraformer：它是非自回归结构，短音频延迟更低，且中英混说（`Agent`、`Docker`、`知识库`）是它的训练覆盖范围。代价是它不支持热词表——FunASR 官方选型文档把「普通话生产流量且需要时间戳或热词」指向 Paraformer-zh。若实测内部术语错误率影响使用，把识别服务内的模型换成 `paraformer-zh` + `fsmn-vad` + `ct-punc` 并接热词表，`/transcribe` 契约不变，前端与后端不需要改动。切换条件写进验证项，不预先实现两套。

## 替代方案

- **浏览器 `SpeechRecognition`**：零部署、零成本，Chrome 走 Google 语音服务。国内网络基本不可用，且无法配置热词、无法控制音频去向，作为产品功能不可接受。
- **云厂商流式 ASR**（讯飞语音听写、阿里 `qwen-audio-3.1-asr-flash-message`、火山豆包流式）：中文准确率最高，讯飞建应用后每日 500 次免费额度。放弃原因是把语音链路绑定到外部账号、额度和网络出口，与本次「零厂商依赖」的取舍相反；免费额度也不适合作为长期产品前提。
- **自托管 Whisper（faster-whisper / whisper.cpp）**：同样免费离线，但中文是明显弱项——短句 CER 8–12%，真实长音频 20%，且输入被补齐到 30 秒帧长，短语音白跑算力。同一份 5 秒音频在 CPU 上要 1–2.5 秒。
- **Meta Omnilingual ASR**：Apache-2.0、1600+ 语种，但只接受 40 秒以内整段音频、不做流式，输出无标点无大小写，CTC 变体不做语种条件（短音频可能落到错误文字系统）。它的目标是长尾语言，不是中文准确率。
- **流式 FunASR（Paraformer 2-pass）**：能边说边出字，但要同时跑实时与离线两路模型，内存与实现复杂度翻倍。用户已明确选择整段识别，本条留作后续演进。
- **把音频当普通附件上传，交给支持音频模态的模型**：现有「添加附件」已允许任意格式 ≤5 MB，技术上可行。但需要模型侧支持音频输入、无法控制识别质量，也不能自动发送，不满足「说完就发」。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 中文短句可转写并自动发出 | 录音成功但消息未发出、或发出的文本与输入框不一致 | `web/src/composables/useSpeechInput.js` | 真实浏览器录一句中文问题，消息列表出现与转写一致的用户消息 | 停掉 `asr` 容器后重试，消息列表不得新增空消息 | Not run |
| 静音自动结束，不产生空消息 | 一直录不停、或无声也发送 | `useSpeechInput.js` 的 RMS 判定 | 说完停顿 1 秒后自动结束；起录后不说话，5 秒取消并提示 | 起录后静默 10 秒，断言未调用 `transcribe` | Not run |
| 音频不被持久化 | 音频或识别文本进入数据库、对象存储或日志 | `speech_service.py`、`chat_router.py` | 转写前后比对 `postgres` 与 MinIO 无新增对象；日志中无音频内容 | 构造一次失败请求，确认失败路径同样无残留 | Not run |
| 超限输入被拒绝 | 超长音频拖垮服务或被静默截断 | `chat_router.py` 的入口校验 | >60 秒 / >5 MB / 非 WAV 三种请求均返回 400，且不调用识别服务 | 逐项构造越界请求断言状态码 | Not run |
| ASR 缺失时可观察降级 | 麦克风按钮可点但永远失败，或文字输入被一起拖坏 | `/api/system/speech/health` + `AgentInputArea.vue` | `docker compose stop asr` 后刷新页面，麦克风按钮不可用，文字输入与发送正常 | 移除 `ASR_API_URI` 配置重启 API，`/ready` 仍为 ready | Not run |
| 权限拒绝不损坏输入框 | 录音失败清空已输入内容 | `useSpeechInput.js` 的错误分支 | 浏览器拒绝麦克风权限后，输入框原文字保留并给出提示 | 拒绝权限后连续点击两次麦克风，不得出现未捕获异常 | Not run |
| 非安全上下文不提供无效入口 | 用 HTTP 访问时按钮仍可点、点了必然失败 | `useSpeechInput.js` 的可用性判定 | HTTPS 入口下按钮可用；同一服务用 `http://<非 localhost>` 访问时按钮不可用 | 在 HTTP 入口断言按钮不存在，且控制台无未捕获异常 | Not run |
| 中文识别质量达到可用 | 术语错到无法阅读，用户需要整句重打 | 识别服务内的模型选择 | 由团队自录 20 条模拟提问（不使用生产会话历史），覆盖 `Agent`、`知识库`、`图谱` 等术语，逐条记字错率与是否需整句重打。判据：平均字错率 ≤8%，且需整句重打的比例 ≤10% | 未达判据时按「已知取舍」切 Paraformer-zh + 热词后重测，重测同样用这 20 条 | Not run |

## 风险

镜像体积增加约 1.5–2 GB，且构建期需要联网下载模型；启用 `voice` profile 的部署要为此付出一次构建与导出成本，离线部署必须走现有的镜像导出/导入流程，不能指望目标机器首次启动时下载。

CPU 推理在多人同时使用时串行排队，并发上限 2 之后直接返回 503。当前没有并发预算数据，按「先可用、再扩容」处理；若实际并发超过预期，需要独立扩容或改为队列化。

SenseVoiceSmall 的权重不采用 MIT/Apache，而是 FunASR Model Open Source License Agreement v1.1。维护者已澄清遵循协议时允许商业使用，但第 2.2 节保留署名与模型名称要求；打包进镜像的决定已确认，署名义务落实为镜像内附协议全文与模型名称，部署文档说明该依赖。

`getUserMedia` 要求安全上下文。生产入口的 HTTPS 由宿主机反向代理提供，[部署文档](../deployment.md)已把 TLS 反代列为对外服务的前置条件，本次不新增 TLS 终止，也不改仓库内 Web 容器的 80 端口监听。桌面端 Electron 壳默认连 `http://10.69.188.181:5173`（`desktop/main.js` 的 `DEFAULT_URL`），该地址不是安全上下文，语音入口在那里不可用；不通过放宽证书校验或把 HTTP 源标记为安全来绕过，桌面端需要指向 HTTPS 地址才有该功能。

iOS Safari 对 `MediaRecorder` 的支持随版本变化，旧版本需隐藏入口而不是报错。这条只能按真实设备验证，桌面浏览器测试不能替代。

静音阈值用固定 RMS 值，不同麦克风与噪声环境表现不一致。首版按实测调整一次，不做自适应校准——自适应会引入新的失败模式，且难以在测试中稳定复现。

不提供流式反馈，用户说完到看见文字之间有等待。按当前模型与 CPU 规模预估 0.5–2 秒，超过 2 秒需要重新评估模型选择，这一点在验收项里以端到端时延记录。
