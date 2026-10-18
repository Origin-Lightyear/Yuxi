import { apiAdminGet, apiAdminPost, apiAdminPut, apiAdminDelete } from './base'

/**
 * IM 机器人配置 API 模块
 * 提供机器人的增删改查与启用状态切换
 */

const BASE_URL = '/api/system/im-bots'

export const imBotApi = {
  /** 获取所有 IM 机器人配置 @returns {Promise} - 机器人列表 */
  list: () => apiAdminGet(BASE_URL),

  /** 创建新的 IM 机器人 @param {Object} data - 机器人配置 @returns {Promise} - 创建结果 */
  create: (data) => apiAdminPost(BASE_URL, data),

  /**
   * 更新指定 IM 机器人配置
   * @param {string} id - 机器人 ID
   * @param {Object} data - 更新数据
   * @returns {Promise} - 更新结果
   */
  update: (id, data) => apiAdminPut(`${BASE_URL}/${id}`, data),

  /** 删除指定 IM 机器人 @param {string} id - 机器人 ID @returns {Promise} - 删除结果 */
  remove: (id) => apiAdminDelete(`${BASE_URL}/${id}`),

  /**
   * 切换指定 IM 机器人的启用状态
   * @param {string} id - 机器人 ID
   * @param {boolean} enabled - 是否启用
   * @returns {Promise} - 切换结果
   */
  setEnabled: (id, enabled) => apiAdminPost(`${BASE_URL}/${id}/enabled?enabled=${enabled}`, {})
}

export default imBotApi
