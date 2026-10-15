import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const panelSource = readFileSync(
  new URL('../../src/components/model-management/AgentManagePanel.vue', import.meta.url),
  'utf8'
)
const chatSource = readFileSync(
  new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
  'utf8'
)
const apiSource = readFileSync(new URL('../../src/apis/agent_api.js', import.meta.url), 'utf8')

test('管理列表放开模板卡片操作插槽并提供另存为副本', () => {
  assert.ok(
    panelSource.includes('v-if="canManageAgent(agent) || agent.is_template"'),
    '插槽 v-if 放开给模板卡片'
  )
  assert.ok(panelSource.includes("name: '模板'"), '渲染模板徽标')
  assert.ok(
    panelSource.includes('agentApi.duplicateAgent(agent.id)'),
    '副本调用传 agent.id（规范身份即 slug）'
  )
  assert.ok(panelSource.includes("name: 'AgentComp'"), '副本创建后跳转对话页')
  assert.ok(
    panelSource.includes('query: { agent_id: newAgent.agent_id || newAgent.slug }'),
    '跳转用响应 agent_id（即 slug），不用数字 newAgent.id'
  )
  assert.ok(!panelSource.includes('agent_id: newAgent.id'), '不再按数字 id 跳转')
  assert.ok(panelSource.includes('message.success'), '副本创建成功有反馈')

  const editGuardIndex = panelSource.indexOf('key="edit"')
  const editVIfIndex = panelSource.lastIndexOf('v-if="canManageAgent(agent)"', editGuardIndex)
  assert.ok(editGuardIndex > 0 && editVIfIndex > 0, '编辑项保留 canManageAgent 判断')

  const duplicateIndex = panelSource.indexOf('agentApi.duplicateAgent(agent.id)')
  const pushIndex = panelSource.indexOf('router.push', duplicateIndex)
  assert.ok(pushIndex > duplicateIndex, '先请求副本再跳转')
})

test('/help 在发送派发前本地拦截', () => {
  const helpIndex = chatSource.indexOf('isHelpCommand(userInput.value)')
  assert.ok(helpIndex > 0, '发送路径顶部存在 /help 拦截')
  assert.ok(chatSource.indexOf('isHelpCommand(userInput.value)', helpIndex + 1) < 0, '拦截只有一处')
  assert.ok(chatSource.includes('helpCardOpen.value = true'), '拦截打开帮助卡片')

  const sendIndex = chatSource.indexOf('await handleSendMessage(payload)')
  assert.ok(sendIndex > helpIndex, '拦截先于发送调用')

  assert.ok(chatSource.includes('v-if="helpCardOpen"'), '帮助卡片按开关渲染')
  assert.ok(
    chatSource.includes('agentStore.loadSceneTemplates()'),
    '卡片打开时按需加载场景模板'
  )
})

test('duplicateAgent 指向按 slug 寻址的副本端点', () => {
  assert.ok(apiSource.includes('duplicateAgent'), 'api 暴露副本请求')
  assert.ok(
    apiSource.includes('apiPost(`/api/agent/${agentId}/duplicate`)'),
    'api 指向 /api/agent/{slug}/duplicate'
  )
})
