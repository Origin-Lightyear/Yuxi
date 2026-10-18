import { apiAdminGet, apiAdminPost, apiAdminPut, apiAdminDelete } from './base'

/**
 * IM 机器人配置 API 模块
 * 提供机器人的增删改查与启用状态切换
 */

const BASE_URL = '/api/system/im-bots'

export const imBotApi = {
  list: () => apiAdminGet(BASE_URL),
  create: (data) => apiAdminPost(BASE_URL, data),
  update: (id, data) => apiAdminPut(`${BASE_URL}/${id}`, data),
  remove: (id) => apiAdminDelete(`${BASE_URL}/${id}`),
  setEnabled: (id, enabled) => apiAdminPost(`${BASE_URL}/${id}/enabled?enabled=${enabled}`, {})
}

export default imBotApi
