import { apiGet, apiRequest } from './base'

/**
 * 语音输入 API：WAV 转写与能力探测。
 */
export const speechApi = {
  /**
   * 上传 16kHz 单声道 WAV，返回转写文本。
   * @param {Blob} wavBlob - 客户端重采样后的 WAV 音频
   * @returns {Promise<{ text: string }>}
   */
  transcribe: (wavBlob) =>
    apiRequest('/api/chat/speech/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'audio/wav' },
      body: wavBlob
    }),

  /**
   * 探测语音服务是否可用，后端自带 30 秒缓存。
   * @returns {Promise<{ available: boolean }>}
   */
  health: () => apiGet('/api/system/speech/health')
}
