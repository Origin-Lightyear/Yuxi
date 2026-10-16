import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const readSource = (relativePath) => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const apiSource = readSource('../../src/apis/agent_memory_api.js')
const panelSource = readSource('../../src/components/AgentMemoryPanel.vue')
const viewSource = readSource('../../src/views/AgentView.vue')

test('agent_memory_api 提供 list 与 remove 并走统一 base 封装', () => {
  assert.match(apiSource, /list\s*[:(]/)
  assert.match(apiSource, /remove\s*[:(]/)
  assert.match(apiSource, /\/api\/agent\/\$\{slug\}\/memories/)
  assert.match(apiSource, /\/api\/agent\/\$\{slug\}\/memories\/\$\{memoryId\}/)
  assert.match(apiSource, /from '\.\/base'/)
  assert.match(apiSource, /apiGet/)
  assert.match(apiSource, /apiDelete/)
})

test('面板调用 agent_memory_api 拉取与删除记忆', () => {
  assert.match(panelSource, /agentMemoryApi/)
  assert.match(panelSource, /\.list\(/)
  assert.match(panelSource, /\.remove\(/)
})

test('面板包含空态文案、删除确认与 enable_memory 门控', () => {
  assert.match(panelSource, /暂无记忆/)
  assert.match(panelSource, /Modal\.confirm/)
  assert.match(panelSource, /enable_memory/)
  assert.match(panelSource, /created_at/)
})

test('AgentView 渲染「记忆」入口并挂载 AgentMemoryPanel', () => {
  assert.match(viewSource, /AgentMemoryPanel/)
  assert.match(viewSource, /记忆/)
  assert.match(viewSource, /Brain/)
  assert.match(viewSource, /enableMemory/)
  assert.match(viewSource, /userConfigApi/)
})
