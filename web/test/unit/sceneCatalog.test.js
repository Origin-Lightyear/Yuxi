import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { Lightbulb } from '@lucide/vue'
import * as Vue from 'vue'
import { renderToString } from 'vue/server-renderer'

const catalogSource = readFileSync(
  new URL('../../src/components/SceneCatalog.vue', import.meta.url),
  'utf8'
)
const chatSource = readFileSync(
  new URL('../../src/components/AgentChatComponent.vue', import.meta.url),
  'utf8'
)
const storeSource = readFileSync(new URL('../../src/stores/agent.js', import.meta.url), 'utf8')
const apiSource = readFileSync(new URL('../../src/apis/agent_api.js', import.meta.url), 'utf8')

const template = catalogSource.slice(
  catalogSource.indexOf('<template>') + '<template>'.length,
  catalogSource.lastIndexOf('</template>')
)
const render = Vue.compile(template)

const scenes = [
  {
    scene: 'writing',
    label: '写作创作',
    templates: [
      {
        agent_id: 'agent-writing',
        slug: 'writing-assistant',
        name: '写作助手',
        description: '润色扩写与风格改写',
        icon: 'pen-line',
        example_questions: ['帮我把这段话写得更正式']
      },
      {
        agent_id: 'agent-weekly',
        slug: 'weekly-report',
        name: '周报生成器',
        description: '汇总一周工作生成周报',
        icon: 'file-text',
        example_questions: []
      }
    ]
  },
  {
    scene: 'research',
    label: '研究分析',
    templates: [
      {
        agent_id: 'agent-research',
        slug: 'deep-research',
        name: '深度研究',
        description: '多源检索与综述',
        icon: 'telescope',
        example_questions: []
      }
    ]
  }
]

const catalogProps = (overrides = {}) => ({
  scenes,
  dailyTip: null,
  sceneIcon: (name) => name,
  ...overrides
})

const renderCatalog = (overrides) =>
  renderToString(
    Vue.createSSRApp({
      components: { Lightbulb },
      data: () => catalogProps(overrides),
      render
    })
  )

const makeNode = (type) => ({ type, children: [], props: {}, parent: null, text: '' })

const renderer = Vue.createRenderer({
  createElement: makeNode,
  createText: (text) => ({ ...makeNode('text'), text }),
  createComment: (text) => ({ ...makeNode('comment'), text }),
  insert(child, parent, anchor = null) {
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(child)
    else parent.children.splice(index, 0, child)
  },
  remove(child) {
    const index = child.parent?.children.indexOf(child) ?? -1
    if (index >= 0) child.parent.children.splice(index, 1)
  },
  setText(node, text) {
    node.text = text
  },
  setElementText(node, text) {
    node.text = text
    node.children = []
  },
  parentNode: (node) => node.parent,
  nextSibling: (node) => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
  patchProp(node, key, _oldValue, value) {
    node.props[key] = value
  }
})

const find = (node, predicate) => {
  if (predicate(node)) return node
  for (const child of node.children || []) {
    const match = find(child, predicate)
    if (match) return match
  }
  return undefined
}

const textContent = (node) =>
  [node.text, ...(node.children || []).map((child) => textContent(child))].join('')

const mountCatalog = (overrides) => {
  const host = makeNode('root')
  const emitted = []
  const app = renderer.createApp({
    components: { Lightbulb },
    data: () => ({
      ...catalogProps(overrides),
      emit: (event, payload) => emitted.push([event, payload])
    }),
    render
  })
  app.mount(host)
  return { app, host, emitted }
}

test('渲染场景分组与模板卡，点卡 emit select', async () => {
  const html = await renderCatalog({})
  assert.ok(html.includes('写作创作'), '渲染分组标签')
  assert.ok(html.includes('研究分析'), '渲染第二个分组')
  assert.ok(html.includes('写作助手'), '渲染模板名称')
  assert.ok(html.includes('润色扩写与风格改写'), '渲染模板描述')

  const { app, host, emitted } = mountCatalog({})
  try {
    const card = find(
      host,
      (node) =>
        node.props.class === 'scene-template-card' && textContent(node).includes('周报生成器')
    )
    assert.ok(card, '模板卡渲染为可点击元素')
    card.props.onClick()
    assert.deepEqual(emitted, [['select', 'agent-weekly']], '点卡 emit select(agent_id)')
  } finally {
    app.unmount()
  }
})

test('dailyTip 为空不渲染横幅，非空渲染且点横幅 emit select', async () => {
  const emptyHtml = await renderCatalog({ scenes: [], dailyTip: null })
  assert.ok(!emptyHtml.includes('scene-daily-tip'), '无每日一技时不渲染横幅')

  const dailyTip = {
    tip: '输入 /help 可随时查看全部能力',
    slug: 'default-chatbot',
    name: '通用助手',
    agent_id: 'agent-daily'
  }
  const html = await renderCatalog({ scenes: [], dailyTip })
  assert.ok(html.includes('scene-daily-tip'), '有每日一技时渲染横幅')
  assert.ok(html.includes('输入 /help 可随时查看全部能力'), '横幅展示技文案')

  const { app, host, emitted } = mountCatalog({ scenes: [], dailyTip })
  try {
    const banner = find(host, (node) => node.props.class === 'scene-daily-tip')
    assert.ok(banner, '横幅渲染为可点击元素')
    banner.props.onClick()
    assert.deepEqual(emitted, [['select', 'agent-daily']], '点横幅 emit select(agent_id)')
  } finally {
    app.unmount()
  }
})

test('AgentChatComponent 挂载目录并仅填充示例问题，不触发发送', () => {
  assert.ok(catalogSource.includes("defineEmits(['select'])"), '目录声明 select 事件')

  const greetIndex = chatSource.indexOf('class="chat-greeting-input"')
  const catalogIndex = chatSource.indexOf('<SceneCatalog')
  const inputIndex = chatSource.indexOf('<AgentInputArea')
  assert.ok(
    greetIndex > 0 && catalogIndex > greetIndex && inputIndex > catalogIndex,
    '目录挂在 greeting 与输入区之间'
  )
  assert.ok(
    chatSource.includes('v-if="isNewConversation && sceneTemplates"'),
    '目录按新会话与数据门控'
  )
  assert.ok(chatSource.includes(':scenes="sceneTemplates"'), '目录接收 scenes')
  assert.ok(chatSource.includes(':daily-tip="dailyTip"'), '目录接收 dailyTip')
  assert.ok(chatSource.includes('@select="agentStore.selectAgent"'), '目录选择接入 selectAgent')

  assert.ok(
    chatSource.includes('v-if="isNewConversation && selectedTemplate?.example_questions?.length"'),
    'chips 仅在新会话且模板问题非空时渲染'
  )
  assert.ok(
    chatSource.includes('@click="fillExampleQuestion(question)"'),
    'chips 点击走填充 handler'
  )

  const fillIndex = chatSource.indexOf('const fillExampleQuestion = ')
  assert.ok(fillIndex > 0, '存在填充 handler')
  const fillBlock = chatSource.slice(fillIndex, chatSource.indexOf('\n}', fillIndex))
  assert.ok(fillBlock.includes('userInput.value = question'), '点击只填充 userInput')
  assert.ok(!/(handleSendOrStop|createAgentRun|@send)/.test(fillBlock), '填充不调用发送路径')

  assert.ok(apiSource.includes('fetchSceneTemplates'), 'api 暴露目录请求')
  assert.ok(apiSource.includes("apiGet('/api/agent/scene-templates')"), 'api 指向场景模板端点')
  assert.ok(storeSource.includes('async function loadSceneTemplates()'), 'store 提供加载动作')
  assert.ok(storeSource.includes('if (sceneTemplates.value) return'), '加载幂等：已加载跳过')
})
