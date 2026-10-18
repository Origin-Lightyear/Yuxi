export function parseAllowedUsers(text) {
  const seen = []
  for (const raw of String(text || '').split(/[\s,，]+/)) {
    const value = raw.trim()
    if (value && !seen.includes(value)) seen.push(value)
  }
  return seen
}

export function buildImBotPayload(form) {
  const payload = {
    platform: form.platform,
    name: form.name,
    owner_uid: form.ownerUid,
    agent_slug: form.agentSlug,
    enabled: form.enabled,
    allowed_users: parseAllowedUsers(form.allowedUsersText),
    require_mention: form.requireMention,
    group_sessions_per_user: form.groupSessionsPerUser
  }
  if (form.credentials) payload.credentials = { ...form.credentials }
  return payload
}

export function formFromImBot(bot) {
  return {
    platform: bot.platform || 'feishu',
    name: bot.name || '',
    ownerUid: bot.owner_uid || '',
    agentSlug: bot.agent_slug || '',
    enabled: bot.enabled !== false,
    allowedUsersText: (bot.allowed_users || []).join(', '),
    requireMention: bot.require_mention !== false,
    groupSessionsPerUser: bot.group_sessions_per_user !== false,
    credentials: null
  }
}
