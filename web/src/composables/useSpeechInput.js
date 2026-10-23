import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { speechApi } from '@/apis/speech_api'
import { SPEECH_RMS_THRESHOLD, isSpeechAvailable, nextRecorderState } from '@/utils/speech_input'

// 转写接口只接受 16kHz 单声道 WAV，重采样固定在客户端完成。
const SPEECH_SAMPLE_RATE = 16000
const SPEECH_ANALYSIS_INTERVAL_MS = 100
const SPEECH_ANALYSIS_FFT_SIZE = 1024

// 后端转写失败的状态码对应到可直接展示的提示。
const SPEECH_ERROR_MESSAGES = {
  400: '音频不符合要求，请重录',
  413: '音频不符合要求，请重录',
  422: '没有识别到内容',
  503: '语音服务不可用',
  504: '识别超时，请重试'
}

/**
 * 语音输入 composable：录音、静音判定、客户端重采样与转写请求。
 *
 * 草稿与发送路径由组件拥有，这里只上报 `{ text, threadIdAtStart }`，
 * 由调用方用 decideTranscriptAction 决定发送、追加还是丢弃。
 *
 * @param {{ getThreadId: () => string, onResult: (result: { text: string, threadIdAtStart: string }) => void, onError: (message: string) => void }} options
 * @returns {{ status: import('vue').Ref<'idle'|'recording'|'transcribing'>, available: import('vue').ComputedRef<boolean>, start: () => Promise<void>, stop: () => Promise<void>, cancel: () => void }}
 */
export function useSpeechInput({ getThreadId, onResult, onError }) {
  const status = ref('idle')
  const healthAvailable = ref(false)
  // 每次取消或重新起录都推进会话号，异步结果只在会话未变时生效。
  let sessionId = 0
  let starting = false
  // 当前录音会话，取消或结束时置空。
  let current = null

  const available = computed(() =>
    isSpeechAvailable({
      secureContext: typeof window !== 'undefined' && Boolean(window.isSecureContext),
      healthAvailable: healthAvailable.value,
      // 输入框禁用状态由组件在渲染处叠加，composable 只判断浏览器与识别服务。
      disabled: false
    })
  )

  /** 请求麦克风并开始录音；任一前置条件不满足都只上报错误，不改变已有输入。 */
  async function start() {
    if (status.value !== 'idle' || starting || !available.value) return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      onError('当前浏览器不支持录音')
      return
    }

    // AudioContext 必须在点击的同一任务内创建，否则可能被自动播放策略挂起而读不到音频。
    let context
    try {
      context = new AudioContext()
    } catch {
      onError('无法启动录音，请检查麦克风')
      return
    }

    starting = true
    const session = ++sessionId
    const threadIdAtStart = getThreadId()
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      })
      // 权限弹窗期间用户已取消或组件已卸载，拿到的轨道直接释放。
      if (session !== sessionId) {
        stopTracks(stream)
        closeContext(context)
        return
      }
      beginSession({ id: session, context, stream, threadIdAtStart })
    } catch {
      closeContext(context)
      if (session === sessionId) onError('无法访问麦克风，请检查浏览器权限')
    } finally {
      starting = false
    }
  }

  /** 手动结束录音并转写。 */
  async function stop() {
    if (status.value !== 'recording') return
    await finishSession()
  }

  /** 取消当前录音或丢弃进行中的转写结果，不产生任何回调成功值。 */
  function cancel() {
    const session = current
    sessionId += 1
    current = null
    if (session) releaseSession(session)
    status.value = 'idle'
  }

  /** 内部取消：停止录音后再告知用户原因。 */
  function cancelWithMessage(text) {
    cancel()
    onError(text)
  }

  /** 建立录音会话：音频图、RMS 采样与 MediaRecorder。 */
  function beginSession(session) {
    try {
      session.context.resume().catch(() => {})
      const analyser = session.context.createAnalyser()
      analyser.fftSize = SPEECH_ANALYSIS_FFT_SIZE
      session.context.createMediaStreamSource(session.stream).connect(analyser)

      const recorder = new MediaRecorder(session.stream)
      const now = performance.now()
      const state = {
        ...session,
        analyser,
        recorder,
        chunks: [],
        samples: new Float32Array(analyser.fftSize),
        timer: null,
        startedAt: now,
        lastSampleAt: now,
        speechDetected: false,
        silentMs: 0
      }

      recorder.ondataavailable = (event) => {
        if (event.data?.size) state.chunks.push(event.data)
      }
      // 设备被拔掉或权限被撤销时按取消处理，避免停在 recording。
      const handleTrackEnded = () => {
        if (current !== state) return
        cancelWithMessage('麦克风已断开，语音输入已取消')
      }
      for (const track of state.stream.getTracks()) {
        track.onended = handleTrackEnded
      }

      recorder.start()
      current = state
      status.value = 'recording'
      state.timer = setInterval(() => sampleSession(state), SPEECH_ANALYSIS_INTERVAL_MS)
    } catch {
      closeContext(session.context)
      stopTracks(session.stream)
      status.value = 'idle'
      onError('无法启动录音，请检查麦克风')
    }
  }

  /** 每 100ms 读取一帧 RMS，交给纯函数决定继续、停止还是取消。 */
  function sampleSession(session) {
    if (current !== session) return
    const now = performance.now()
    session.analyser.getFloatTimeDomainData(session.samples)

    let sum = 0
    for (const value of session.samples) sum += value * value
    const rms = Math.sqrt(sum / session.samples.length)

    if (rms >= SPEECH_RMS_THRESHOLD) {
      session.speechDetected = true
      session.silentMs = 0
    } else if (session.speechDetected) {
      session.silentMs += now - session.lastSampleAt
    }
    session.lastSampleAt = now

    const action = nextRecorderState({
      elapsedMs: now - session.startedAt,
      speechDetected: session.speechDetected,
      silentMs: session.silentMs,
      rms
    })
    if (action === 'stop') {
      finishSession()
    } else if (action === 'cancel') {
      cancelWithMessage('没有听到声音，请再试一次')
    }
  }

  /** 停止录音、释放资源并转写；会话已被取消时不再发起请求。 */
  async function finishSession() {
    const session = current
    if (!session) return
    current = null
    status.value = 'transcribing'
    try {
      clearInterval(session.timer)
      session.timer = null
      detachTrackHandlers(session)

      await stopRecorder(session)
      const blob = new Blob(session.chunks, { type: session.recorder.mimeType || 'audio/webm' })
      releaseSession(session)
      if (session.id !== sessionId) return
      await transcribeChunks(session.id, session.threadIdAtStart, blob)
    } catch {
      releaseSession(session)
      if (session.id === sessionId) {
        status.value = 'idle'
        onError('语音识别失败，请重试')
      }
    }
  }

  /** 上传音频并把结果交给调用方；失败按状态码给出可读提示并回到 idle。 */
  async function transcribeChunks(id, threadIdAtStart, blob) {
    try {
      if (!blob.size) {
        onError('没有录到音频，请重试')
        return
      }

      let wav
      try {
        wav = await toWavBlob(blob)
      } catch {
        onError('音频处理失败，请重录')
        return
      }
      if (id !== sessionId) return

      const result = await speechApi.transcribe(wav)
      if (id !== sessionId) return

      const text = typeof result?.text === 'string' ? result.text.trim() : ''
      if (!text) {
        onError('没有识别到内容')
        return
      }
      onResult({ text, threadIdAtStart })
    } catch (error) {
      if (id !== sessionId) return
      onError(SPEECH_ERROR_MESSAGES[error?.status] || '语音识别失败，请重试')
    } finally {
      if (id === sessionId) status.value = 'idle'
    }
  }

  onMounted(async () => {
    try {
      const result = await speechApi.health()
      healthAvailable.value = Boolean(result?.available)
    } catch {
      healthAvailable.value = false
    }
  })

  // 组件卸载时释放麦克风与音频上下文，丢弃未落地的转写。
  onBeforeUnmount(() => cancel())

  return { status, available, start, stop, cancel }
}

/** 释放会话持有的轨道、音频上下文与计时器；可重复调用。 */
function releaseSession(session) {
  if (session.timer) {
    clearInterval(session.timer)
    session.timer = null
  }
  detachTrackHandlers(session)
  if (session.recorder?.state !== 'inactive') {
    try {
      session.recorder.stop()
    } catch {
      // 已停止或从未启动的 recorder 无需再停。
    }
  }
  if (session.stream) {
    stopTracks(session.stream)
    session.stream = null
  }
  if (session.context) {
    closeContext(session.context)
    session.context = null
  }
  session.analyser = null
}

function detachTrackHandlers(session) {
  if (!session.stream) return
  for (const track of session.stream.getTracks()) track.onended = null
}

function closeContext(context) {
  if (context.state !== 'closed') context.close().catch(() => {})
}

function stopTracks(stream) {
  for (const track of stream.getTracks()) track.stop()
}

/** 等待 MediaRecorder 落盘最后一个数据块。 */
function stopRecorder(session) {
  if (session.recorder.state === 'inactive') return Promise.resolve()
  return new Promise((resolve) => {
    session.recorder.addEventListener('stop', () => resolve(), { once: true })
    session.recorder.stop()
  })
}

/** 解码任意浏览器录音格式，重采样为 16kHz 单声道并编码成 WAV。 */
async function toWavBlob(blob) {
  const arrayBuffer = await blob.arrayBuffer()
  const decodeContext = new AudioContext()
  try {
    const audioBuffer = await decodeContext.decodeAudioData(arrayBuffer)
    const frames = Math.ceil(audioBuffer.duration * SPEECH_SAMPLE_RATE)
    if (!frames) throw new Error('empty audio')

    const offline = new OfflineAudioContext(1, frames, SPEECH_SAMPLE_RATE)
    const source = offline.createBufferSource()
    source.buffer = audioBuffer
    source.connect(offline.destination)
    source.start()

    const rendered = await offline.startRendering()
    return encodeWavPcm16(rendered.getChannelData(0), SPEECH_SAMPLE_RATE)
  } finally {
    decodeContext.close().catch(() => {})
  }
}

/** 单声道 16-bit PCM WAV。 */
function encodeWavPcm16(samples, sampleRate) {
  const dataLength = samples.length * 2
  const buffer = new ArrayBuffer(44 + dataLength)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataLength, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataLength, true)

  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample))
    view.setInt16(44 + index * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
  })

  return new Blob([buffer], { type: 'audio/wav' })
}

function writeAscii(view, offset, text) {
  for (let index = 0; index < text.length; index += 1) {
    view.setUint8(offset + index, text.charCodeAt(index))
  }
}
