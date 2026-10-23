/**
 * 语音输入的纯判定规则：入口可用性、录音停止时机与转写结果处理。
 *
 * 模块不访问 window / navigator / 计时器 / DOM，浏览器能力由调用方探测后传入，
 * 因此可在 Node 中直接单测；MediaRecorder 与 WebAudio 的接线由调用方负责。
 */

// 检测到语音后持续静音的容忍时长，超过即认为一句话说完
export const SPEECH_SILENCE_MS = 800

// 起录后始终未检测到语音的容忍时长，超过即视为误触并取消
export const SPEECH_NO_SPEECH_CANCEL_MS = 5000

// 单次录音上限，与后端转写接口的时长限制保持一致
export const SPEECH_MAX_RECORD_MS = 60000

// 判断当前音频帧是否为语音的 RMS 阈值，调用方用它推导 speechDetected
export const SPEECH_RMS_THRESHOLD = 0.02

/**
 * 判断当前环境是否提供语音输入入口。
 *
 * 非安全上下文拿不到麦克风；识别服务不可用或输入框禁用时同样不展示入口。
 *
 * @param {{ secureContext: boolean, healthAvailable: boolean, disabled: boolean }} params
 * @returns {boolean} 三项均成立才为 true
 */
export function isSpeechAvailable({ secureContext, healthAvailable, disabled }) {
  return Boolean(secureContext && healthAvailable && !disabled)
}

/**
 * 决定录音的下一步状态。
 *
 * 判定顺序影响结果：到达录音上限先强制停止；始终无语音时取消；有语音后静音超时才停止。
 *
 * @param {{ elapsedMs: number, speechDetected: boolean, silentMs: number, rms: number }} params
 *   rms 仅为调用方采集侧保留，判定不读取；speechDetected 已由阈值换算得出。
 * @returns {'continue' | 'stop' | 'cancel'}
 */
export function nextRecorderState({ elapsedMs, speechDetected, silentMs }) {
  if (elapsedMs >= SPEECH_MAX_RECORD_MS) return 'stop'
  if (!speechDetected && elapsedMs >= SPEECH_NO_SPEECH_CANCEL_MS) return 'cancel'
  if (speechDetected && silentMs >= SPEECH_SILENCE_MS) return 'stop'
  return 'continue'
}

/**
 * 决定一段转写文本的落点。
 *
 * 会话已切换时文本属于旧会话，即使草稿非空也丢弃，避免串到新会话。
 *
 * @param {{ draft: string, threadIdAtStart: string, currentThreadId: string }} params
 * @returns {'send' | 'append' | 'discard'}
 */
export function decideTranscriptAction({ draft, threadIdAtStart, currentThreadId }) {
  if (threadIdAtStart !== currentThreadId) return 'discard'
  return draft ? 'append' : 'send'
}
