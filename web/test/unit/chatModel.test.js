import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveAvailableChatModelSpec } from '../../src/utils/chatModel.js'

test('下线的会话选择回退到仍可用的 Agent 默认模型', () => {
  const resolved = resolveAvailableChatModelSpec({
    selectedModel: 'retired:model',
    agentModel: 'available:agent-model',
    systemModel: 'available:system-model',
    availableSpecs: ['available:agent-model', 'available:system-model']
  })

  assert.equal(resolved, 'available:agent-model')
})

test('Agent 与系统默认模型均下线时回退到首个可用模型', () => {
  const resolved = resolveAvailableChatModelSpec({
    selectedModel: '',
    agentModel: 'retired:agent-model',
    systemModel: 'retired:system-model',
    availableSpecs: ['available:first-model', 'available:second-model']
  })

  assert.equal(resolved, 'available:first-model')
})

test('没有可用聊天模型时返回空值', () => {
  const resolved = resolveAvailableChatModelSpec({
    selectedModel: 'retired:model',
    agentModel: 'retired:agent-model',
    systemModel: 'retired:system-model',
    availableSpecs: []
  })

  assert.equal(resolved, '')
})
