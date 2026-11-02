/**
 * 将用户输入的文本解析为允许用户列表，按空白、英文/中文逗号切分并去重
 * @param {string} text - 原始文本，可用空白或中英文逗号分隔
 * @returns {string[]} - 去重后的用户标识列表
 */
export function parseAllowedUsers(text) {
  const seen = []
  for (const raw of String(text || '').split(/[\s,，]+/)) {
    const value = raw.trim()
    if (value && !seen.includes(value)) seen.push(value)
  }
  return seen
}

/**
 * 将表单模型转换为后端接口所需的机器人配置载荷
 * @param {Object} form - 表单数据
 * @returns {Object} - 后端机器人配置对象，仅在填写凭据时附带 credentials
 */
export function buildImBotPayload(form) {
  const payload = {
    platform: form.platform,
    name: form.name,
    owner_uid: form.ownerUid,
    agent_slug: form.agentSlug,
    enabled: form.enabled,
    allowed_users: parseAllowedUsers(form.allowedUsersText),
    require_mention: form.requireMention,
    require_binding: form.requireBinding,
    group_sessions_per_user: form.groupSessionsPerUser
  }
  if (form.credentials) payload.credentials = { ...form.credentials }
  return payload
}

/**
 * 将后端机器人配置还原为表单模型，凭据不回填（需重新输入）
 * @param {Object} bot - 后端返回的机器人配置
 * @returns {Object} - 表单数据，凭据固定为 null
 */
export function formFromImBot(bot) {
  return {
    platform: bot.platform || 'feishu',
    name: bot.name || '',
    ownerUid: bot.owner_uid || '',
    agentSlug: bot.agent_slug || '',
    enabled: bot.enabled !== false,
    allowedUsersText: (bot.allowed_users || []).join(', '),
    requireMention: bot.require_mention !== false,
    requireBinding: bot.require_binding === true,
    groupSessionsPerUser: bot.group_sessions_per_user !== false,
    credentials: null
  }
}
