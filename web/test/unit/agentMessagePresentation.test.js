import assert from 'node:assert/strict'
import path from 'node:path'
import { after, before, test } from 'node:test'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { createPinia } from 'pinia'
import { createRenderer, createSSRApp, h, nextTick, ref } from 'vue'
import { createServer } from 'vite'

import {
  resolveReasoningPresentation,
  resolveToolPresentation
} from '../../src/utils/agentMessagePresentation.js'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
let server
let AgentMessageComponent
let renderToString

before(async () => {
  const storage = new Map()
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key)
  }
  server = await createServer({
    root: webRoot,
    server: { middlewareMode: true },
    plugins: [
      {
        name: 'agent-message-test-stubs',
        enforce: 'pre',
        resolveId(source) {
          if (source.endsWith('/components/common/MarkdownPreview.vue')) {
            return '\0agent-message-markdown-stub'
          }
          return null
        },
        load(id) {
          if (id === '\0agent-message-markdown-stub') {
            return 'export default { name: "MarkdownPreviewStub", render: () => null }'
          }
          return null
        }
      }
    ]
  })
  ;({ default: AgentMessageComponent } = await server.ssrLoadModule(
    '/src/components/AgentMessageComponent.vue'
  ))
  const require = createRequire(import.meta.url)
  const vueRequire = createRequire(require.resolve('vue'))
  ;({ renderToString } = await import(pathToFileURL(vueRequire.resolve('@vue/server-renderer'))))
})

after(async () => {
  await server?.close()
  delete globalThis.localStorage
})

const renderAgentMessage = (props) => {
  const app = createSSRApp({ render: () => h(AgentMessageComponent, props) })
  app.use(createPinia())
  return renderToString(app)
}

const stateRenderer = createRenderer({
  patchProp: () => {},
  insert: (child, parent) => parent.children.push(child),
  remove: () => {},
  createElement: (type) => ({ type, children: [] }),
  createText: (text) => ({ type: 'text', text }),
  createComment: (text) => ({ type: 'comment', text }),
  setText: () => {},
  setElementText: () => {},
  parentNode: () => null,
  nextSibling: () => null,
  querySelector: () => null,
  setScopeId: () => {},
  cloneNode: (node) => ({ ...node }),
  insertStaticContent: () => [null, null]
})

const mountReasoningState = (message) => {
  const currentMessage = ref(message)
  const isProcessing = ref(true)
  let state
  const probe = {
    props: AgentMessageComponent.props,
    setup(props, context) {
      state = AgentMessageComponent.setup(props, context)
      return () => h('div')
    }
  }
  const app = stateRenderer.createApp({
    setup: () => () =>
      h(probe, {
        message: currentMessage.value,
        isProcessing: isProcessing.value,
        hideToolCalls: true
      })
  })
  app.use(createPinia())
  app.provide(Symbol.for('v-scx'), { modules: new Set() })
  app.mount({ children: [] })
  return { currentMessage, state }
}

test('completed reasoning is collapsed by default', () => {
  assert.deepEqual(resolveReasoningPresentation({ status: 'finished' }), {
    active: false,
    expanded: false
  })
})

test('only the active task is expanded while a run is executing', () => {
  const state = resolveToolPresentation([
    { id: 'old', status: 'success' },
    { id: 'current', status: 'running' }
  ])

  assert.deepEqual(
    state.filter((item) => item.expanded).map((item) => item.id),
    ['current']
  )
})

test('reasoning stays visible while processing and collapses after finish', async () => {
  const message = {
    id: 'reasoning-message',
    type: 'ai',
    status: 'loading',
    additional_kwargs: { reasoning_content: '正在分析' }
  }

  const activeHtml = await renderAgentMessage({ message, isProcessing: true, hideToolCalls: true })
  assert.match(activeHtml, /reasoning-panel/)
  assert.match(activeHtml, /正在分析/)

  const finishedHtml = await renderAgentMessage({
    message: { ...message, status: 'finished' },
    isProcessing: false,
    hideToolCalls: true
  })
  assert.doesNotMatch(finishedHtml, /reasoning-panel/)
  assert.match(finishedHtml, /reasoning-summary/)
  assert.doesNotMatch(finishedHtml, / disabled/)
})

test('an unresolved ordinary tool collapses when the whole run ends', () => {
  const toolCalls = [{ id: 'unresolved', status: 'running' }]
  const activeState = resolveToolPresentation(toolCalls, new Set(), true)
  const finishedState = resolveToolPresentation(toolCalls, new Set(), false)

  assert.deepEqual(
    activeState.filter((item) => item.expanded).map((item) => item.id),
    ['unresolved']
  )
  assert.deepEqual(
    finishedState.filter((item) => item.expanded).map((item) => item.id),
    []
  )
})

test('active subagent ids cannot expand tools after the whole run ends', () => {
  const state = resolveToolPresentation(
    [{ id: 'subagent-current', status: 'running' }],
    new Set(['subagent-current']),
    false
  )

  assert.deepEqual(
    state.filter((item) => item.expanded).map((item) => item.id),
    []
  )
})

test('reasoning collapses when the same message advances to a tool call', async () => {
  const reasoningMessage = {
    id: 'phase-message',
    type: 'ai',
    status: 'loading',
    additional_kwargs: { reasoning_content: '先分析问题' }
  }
  const mounted = mountReasoningState(reasoningMessage)
  assert.equal(mounted.state.reasoningExpanded.value, true)
  mounted.currentMessage.value = {
    ...reasoningMessage,
    tool_calls: [{ id: 'search', name: 'web_search', args: { query: 'test' } }]
  }
  await nextTick()
  assert.equal(mounted.state.reasoningExpanded.value, false)
})

test('reasoning collapses when the same message advances to answer content', async () => {
  const reasoningMessage = {
    id: 'phase-message',
    type: 'ai',
    status: 'loading',
    additional_kwargs: { reasoning_content: '先分析问题' }
  }
  const mounted = mountReasoningState(reasoningMessage)
  assert.equal(mounted.state.reasoningExpanded.value, true)
  mounted.currentMessage.value = { ...reasoningMessage, content: '最终回答' }
  await nextTick()
  assert.equal(mounted.state.reasoningExpanded.value, false)
})
