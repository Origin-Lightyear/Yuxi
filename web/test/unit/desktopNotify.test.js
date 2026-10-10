// web/test/unit/desktopNotify.test.js
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

// 静态 import 的 chatThreads store 依赖链在模块求值时读取 localStorage（与 chatThreadsStore.test.js 相同）
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

// 惰性单例：与 agentRequestQueue.test.js 相同的 ssrLoadModule 模式（模块级 promise，无 before 钩子）
let modPromise
let server
const loadMod = () =>
  (modPromise ??= createServer({ server: { middlewareMode: true }, appType: 'custom' }).then(async (s) => {
    server = s
    const mod = await s.ssrLoadModule('/src/utils/desktopNotify.js')
    return mod
  }))

// middleware 模式的 vite server 仍持有 watcher 句柄，不关闭则测试进程无法退出
after(() => server?.close())

async function setup({ desktop = true, focused = false } = {}) {
  const { notifyRunTerminal } = await loadMod()
  const created = []
  globalThis.window = desktop ? { yuxiDesktop: {}, focus() {} } : { focus() {} }
  globalThis.document = { hasFocus: () => focused }
  globalThis.Notification = class {
    constructor(title, opts) {
      this.title = title
      this.body = opts.body
      created.push(this)
    }
  }
  setActivePinia(createPinia())
  return { notifyRunTerminal, created }
}

test('非桌面环境不构造 Notification', async () => {
  const { notifyRunTerminal, created } = await setup({ desktop: false })
  assert.equal(notifyRunTerminal({ threadId: 't1', status: 'completed' }), false)
  assert.equal(created.length, 0)
})
test('聚焦时不构造 Notification', async () => {
  const { notifyRunTerminal, created } = await setup({ focused: true })
  assert.equal(notifyRunTerminal({ threadId: 't1', status: 'completed' }), false)
  assert.equal(created.length, 0)
})
test('未知状态不构造 Notification', async () => {
  const { notifyRunTerminal, created } = await setup()
  assert.equal(notifyRunTerminal({ threadId: 't1', status: 'interrupted' }), false)
  assert.equal(created.length, 0)
})
test('三终态文案映射', async () => {
  const { notifyRunTerminal, created } = await setup()
  const terminal = { completed: '已完成', failed: '执行失败', cancelled: '已取消' }
  for (const [status, body] of Object.entries(terminal)) {
    assert.equal(notifyRunTerminal({ threadId: 't1', status }), true)
    assert.equal(created.at(-1).body, body)
  }
})
test('点击通知回焦；无 threadId 点击不跳转', async () => {
  const { notifyRunTerminal, created } = await setup()
  let focused = 0
  let assigned = null
  globalThis.window.focus = () => { focused += 1 }
  globalThis.window.location = { assign: (u) => { assigned = u } }
  notifyRunTerminal({ threadId: 't1', status: 'failed' })
  created[0].onclick()
  assert.equal(focused, 1)
  assert.equal(assigned, '/agent/t1')
  const second = await setup()
  second.notifyRunTerminal({ status: 'completed' })
  second.created[0].onclick()
  assert.equal(assigned, '/agent/t1') // 无 threadId 不再调用 assign
})
