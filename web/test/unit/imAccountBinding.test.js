import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'
import { createServer } from 'vite'

const makeNode = (type) => ({ type, children: [], props: {}, style: {}, parent: null, text: '' })

const renderer = createRenderer({
  createElement: makeNode,
  createText(text) {
    return { ...makeNode('text'), text }
  },
  createComment(text) {
    return { ...makeNode('comment'), text }
  },
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

const flush = async () => {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

async function createComponentServer() {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    ssr: { noExternal: ['ant-design-vue', 'qrcode'] },
    plugins: [
      {
        name: 'im-account-binding-test',
        enforce: 'pre',
        resolveId(id) {
          if (id === 'ant-design-vue') return '\0test-antd'
          if (id === 'qrcode') return '\0test-qrcode'
          if (id === 'virtual:im-account-binding') return '\0virtual:im-account-binding'
          return null
        },
        load(id) {
          if (id === '\0test-antd') {
            return 'export const message={error(){},success(){},warning(){}};' +
              'export const notification={error(){},success(){}};' +
              'export const Modal={confirm(){}}'
          }
          if (id === '\0test-qrcode') {
            return 'export const toDataURL=(text)=>Promise.resolve("data:image/png;base64,QR:"+text)'
          }
          if (id === '\0virtual:im-account-binding') {
            const source = readFileSync(
              new URL('../../src/components/ImAccountBindingComponent.vue', import.meta.url),
              'utf8'
            )
            const { descriptor } = parse(source)
            return compileScript(descriptor, { id: 'im-account-binding', inlineTemplate: true }).content
          }
          if (id.endsWith('/src/apis/im_binding_api.js')) {
            return 'export const imBindingApi = globalThis.__imBindingApi'
          }
          return null
        }
      }
    ]
  })

  const { default: ImAccountBinding } = await server.ssrLoadModule('virtual:im-account-binding')

  return { server, ImAccountBinding }
}

async function mountBinding(imBindingApi) {
  globalThis.__imBindingApi = imBindingApi
  const { server, ImAccountBinding } = await createComponentServer()

  const app = renderer.createApp(() => h(ImAccountBinding))
  app.component('a-button', {
    inheritAttrs: false,
    setup(_, { slots, attrs }) {
      return () => h('button', { ...attrs }, slots.default?.())
    }
  })
  app.component('a-modal', {
    inheritAttrs: false,
    props: ['open'],
    emits: ['cancel', 'update:open'],
    setup(props, { slots }) {
      return () => (props.open ? h('div', { class: 'bind-modal' }, slots.default?.()) : null)
    }
  })
  app.component('a-spin', {
    setup(_, { slots }) {
      return () => h('div', { class: 'spin' }, slots.default?.())
    }
  })
  app.component('a-alert', {
    setup(_, { slots }) {
      return () => h('div', { class: 'alert' }, slots.default?.())
    }
  })

  const host = makeNode('root')
  app.mount(host)
  await flush()

  return { host, app, server }
}

test('无绑定时渲染绑定按钮，点击调用 createSession 并把 bind_url 渲染为二维码', async () => {
  const sessions = []
  const imBindingApi = {
    list: async () => ({ success: true, data: [] }),
    createSession: async (platform) => {
      sessions.push(platform)
      return { success: true, data: { bind_url: 'https://bind.example/feishu', expires_at: 'x' } }
    },
    unbind: async () => ({ success: true, data: { deleted: true } })
  }

  let ctx
  try {
    ctx = await mountBinding(imBindingApi)
    const { host } = ctx

    const bindFeishu = find(
      host,
      (node) => node.type === 'button' && textContent(node).trim() === '绑定飞书'
    )
    const bindDingtalk = find(
      host,
      (node) => node.type === 'button' && textContent(node).trim() === '绑定钉钉'
    )
    assert.ok(bindFeishu, '应渲染绑定飞书按钮')
    assert.ok(bindDingtalk, '应渲染绑定钉钉按钮')
    assert.equal(
      find(host, (node) => node.type === 'button' && textContent(node).trim() === '解绑'),
      undefined
    )

    bindFeishu.props.onClick()
    await flush()

    assert.deepEqual(sessions, ['feishu'])
    const img = find(host, (node) => node.type === 'img')
    assert.ok(img, '应渲染二维码图片')
    assert.equal(img.props.src, 'data:image/png;base64,QR:https://bind.example/feishu')
  } finally {
    ctx?.app.unmount()
    await ctx?.server.close()
    delete globalThis.__imBindingApi
  }
})

test('已绑定时显示可解绑状态', async () => {
  const imBindingApi = {
    list: async () => ({
      success: true,
      data: [{ platform: 'feishu', platform_user_id: 'ou_123', created_at: '2026-01-01T00:00:00Z' }]
    }),
    createSession: async () => ({ success: true, data: {} }),
    unbind: async () => ({ success: true, data: { deleted: true } })
  }

  let ctx
  try {
    ctx = await mountBinding(imBindingApi)
    const { host } = ctx

    assert.ok(find(host, (node) => textContent(node).trim() === '已绑定：ou_123'), '应显示已绑定账号')
    assert.ok(
      find(host, (node) => node.type === 'button' && textContent(node).trim() === '解绑'),
      '应渲染解绑按钮'
    )
    assert.ok(
      find(host, (node) => node.type === 'button' && textContent(node).trim() === '绑定钉钉'),
      '未绑定平台仍渲染绑定按钮'
    )
  } finally {
    ctx?.app.unmount()
    await ctx?.server.close()
    delete globalThis.__imBindingApi
  }
})
