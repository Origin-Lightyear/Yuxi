import { apiDelete, apiGet, apiPost } from './base'

/**
 * IM 账号绑定 API 模块（普通用户接口）
 * 提供绑定列表、发起绑定会话、解绑三类操作
 */

const BASE_URL = '/api/user/im-bindings'

export const imBindingApi = {
  /** 获取当前用户全部 IM 绑定 @returns {Promise} - { success, data: [{ platform, platform_user_id, created_at }] } */
  list: () => apiGet(BASE_URL),

  /**
   * 发起某平台的绑定会话，返回扫码地址
   * @param {string} platform - 'feishu' | 'dingtalk'
   * @returns {Promise} - { success, data: { bind_url, expires_at } }
   */
  createSession: (platform) => apiPost(`${BASE_URL}/${platform}/sessions`),

  /** 解绑指定平台 @param {string} platform - 'feishu' | 'dingtalk' @returns {Promise} */
  unbind: (platform) => apiDelete(`${BASE_URL}/${platform}`)
}

export default imBindingApi
