import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildImBotPayload, formFromImBot, parseAllowedUsers } from '../../src/utils/imBotForm.js'

test('parseAllowedUsers splits, trims and dedupes', () => {
  assert.deepEqual(parseAllowedUsers('ou_a, ou_a\nou_b'), ['ou_a', 'ou_b'])
})

test('buildImBotPayload maps form fields', () => {
  const payload = buildImBotPayload({
    platform: 'feishu',
    name: 'bot',
    ownerUid: 'u1',
    agentSlug: 'default-chatbot',
    enabled: true,
    allowedUsersText: 'ou_a,ou_b',
    requireMention: true,
    requireBinding: true,
    groupSessionsPerUser: true,
    credentials: { app_id: 'a', app_secret: 'b' }
  })
  assert.equal(payload.owner_uid, 'u1')
  assert.deepEqual(payload.allowed_users, ['ou_a', 'ou_b'])
  assert.equal(payload.require_mention, true)
  assert.equal(payload.require_binding, true)
  assert.equal(payload.credentials.app_id, 'a')
})

test('formFromImBot omits credentials and joins users', () => {
  const form = formFromImBot({
    platform: 'dingtalk',
    name: 'bot',
    owner_uid: 'u2',
    agent_slug: 'x',
    enabled: false,
    allowed_users: ['a', 'b'],
    require_mention: false,
    require_binding: true,
    group_sessions_per_user: false
  })
  assert.equal(form.ownerUid, 'u2')
  assert.equal(form.allowedUsersText, 'a, b')
  assert.equal(form.platform, 'dingtalk')
  assert.equal(form.requireBinding, true)
})

test('formFromImBot defaults requireBinding to false when unset', () => {
  assert.equal(formFromImBot({}).requireBinding, false)
})
