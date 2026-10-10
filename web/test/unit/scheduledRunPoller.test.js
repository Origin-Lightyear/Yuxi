// web/test/unit/scheduledRunPoller.test.js
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { createServer } from 'vite'

// poller 静态 import 链（scheduledAgentApi、desktopNotify → stores）在模块求值时读取 localStorage（与 desktopNotify.test.js 相同）
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

// 惰性单例：与 desktopNotify.test.js 相同的 ssrLoadModule 模式（模块级 promise，无 before 钩子）
let modPromise
let server
const loadMod = () =>
  (modPromise ??= createServer({ server: { middlewareMode: true }, appType: 'custom' }).then(async (s) => {
    server = s
    return s.ssrLoadModule('/src/utils/scheduledRunPoller.js')
  }))

// middleware 模式的 vite server 仍持有 watcher 句柄，不关闭则测试进程无法退出
after(() => server?.close())

const job = (runs) => ({ jobs: [{ name: '月度经营分析', runs }] })
const flush = () => setImmediate()

// Node 的 mock timers 不暴露 timerCount：启用 mock setInterval 后，经注入的 setTimer/clearTimer 统计活动定时器与间隔时长
const trackTimers = (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  const active = new Set()
  const delays = []
  return {
    setTimer: (fn, ms) => {
      const id = setInterval(fn, ms)
      active.add(id)
      delays.push(ms)
      return id
    },
    clearTimer: (id) => {
      active.delete(id)
      clearInterval(id)
    },
    timerCount: () => active.size,
    delays
  }
}

test('非桌面或初始聚焦时不启动定时器', async (t) => {
  const timers = trackTimers(t)
  // 首次模块求值必须发生在伪 document 之前：runtime-dom 求值期会读取 document.createElement
  const { createScheduledRunPoller } = await loadMod()
  const registered = []
  globalThis.window = {
    addEventListener: (type) => registered.push(type)
  }
  globalThis.document = { hasFocus: () => false }
  const listed = []
  const deps = {
    list: async () => {
      listed.push(1)
      return job([])
    },
    notify: () => {},
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer
  }
  const p = createScheduledRunPoller(deps)
  p.init()
  t.mock.timers.tick(30000)
  await flush()
  assert.equal(timers.timerCount(), 0)
  assert.equal(listed.length, 0)
  assert.deepEqual(registered, []) // 非 Electron 环境零副作用

  globalThis.window = { yuxiDesktop: {}, addEventListener: (type) => registered.push(type) }
  globalThis.document = { hasFocus: () => true }
  const focused = createScheduledRunPoller(deps)
  focused.init()
  t.mock.timers.tick(30000)
  await flush()
  assert.equal(timers.timerCount(), 0)
  assert.equal(listed.length, 0)
  assert.deepEqual(registered, ['blur', 'focus']) // 初始聚焦的桌面壳只注册监听，不启动轮询
})

test('首 tick 只建快照不通知，后续新终态才通知', async (t) => {
  const timers = trackTimers(t)
  globalThis.window = { yuxiDesktop: {} }
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const notified = []
  let payload = job([{ id: 'r1', thread_id: 't1', status: 'running' }])
  const p = createScheduledRunPoller({
    list: async () => payload,
    notify: (o) => notified.push(o),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer
  })
  p.init() // 初始失焦 → 立即 tick（seed）
  await flush()
  assert.equal(timers.timerCount(), 1)
  assert.deepEqual(timers.delays, [30000])
  t.mock.timers.tick(30000)
  await flush()
  assert.equal(notified.length, 0)
  payload = job([{ id: 'r1', thread_id: 't1', status: 'completed' }])
  t.mock.timers.tick(30000)
  await flush()
  assert.equal(notified.length, 1)
  assert.deepEqual(notified[0], { title: '月度经营分析', threadId: 't1', status: 'completed' })
})

test('聚焦停止定时器且旧终态不重发', async (t) => {
  const timers = trackTimers(t)
  const handlers = {}
  globalThis.window = {
    yuxiDesktop: {},
    addEventListener: (type, fn) => {
      handlers[type] = fn
    },
    removeEventListener: (type) => {
      delete handlers[type]
    }
  }
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const notified = []
  const payload = job([{ id: 'r1', thread_id: 't1', status: 'completed' }])
  const p = createScheduledRunPoller({
    list: async () => payload,
    notify: (o) => notified.push(o),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer
  })
  p.init()
  await flush()
  assert.equal(notified.length, 0) // 首 tick 只 seed
  assert.equal(timers.timerCount(), 1)
  handlers.focus()
  assert.equal(timers.timerCount(), 0)
  handlers.blur()
  assert.equal(timers.timerCount(), 1)
  await flush()
  assert.equal(notified.length, 0) // r1 已是快照终态，不重复通知
})

test('非三终态变化不通知', async (t) => {
  const timers = trackTimers(t)
  globalThis.window = { yuxiDesktop: {} }
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const notified = []
  let payload = job([{ id: 'r1', thread_id: 't1', status: 'running' }])
  const p = createScheduledRunPoller({
    list: async () => payload,
    notify: (o) => notified.push(o),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer
  })
  p.init()
  await flush()
  payload = job([{ id: 'r1', thread_id: 't1', status: 'interrupted' }])
  t.mock.timers.tick(30000)
  await flush()
  payload = job([{ id: 'r1', thread_id: 't1', status: 'running' }])
  t.mock.timers.tick(30000)
  await flush()
  assert.equal(notified.length, 0)
})

test('failed 与 cancelled 终态也通知且按 run.id 去重', async (t) => {
  const timers = trackTimers(t)
  globalThis.window = { yuxiDesktop: {} }
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const notified = []
  let payload = job([{ id: 'r1', thread_id: 't1', status: 'running' }])
  const p = createScheduledRunPoller({
    list: async () => payload,
    notify: (o) => notified.push(o),
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer
  })
  p.init()
  await flush()
  payload = job([{ id: 'r1', thread_id: 't1', status: 'failed' }])
  t.mock.timers.tick(30000)
  await flush()
  payload = job([{ id: 'r1', thread_id: 't1', status: 'failed' }])
  t.mock.timers.tick(30000)
  await flush()
  payload = job([{ id: 'r2', thread_id: 't2', status: 'cancelled' }])
  t.mock.timers.tick(30000)
  await flush()
  assert.deepEqual(
    notified.map((n) => n.status),
    ['failed', 'cancelled']
  )
})

test('stop 清定时器与监听且 init 可重新启动', async (t) => {
  const timers = trackTimers(t)
  const handlers = {}
  globalThis.window = {
    yuxiDesktop: {},
    addEventListener: (type, fn) => {
      handlers[type] = fn
    },
    removeEventListener: (type) => {
      delete handlers[type]
    }
  }
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const p = createScheduledRunPoller({
    list: async () => job([]),
    notify: () => {},
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer
  })
  p.init()
  await flush()
  assert.equal(timers.timerCount(), 1)
  assert.deepEqual(Object.keys(handlers).sort(), ['blur', 'focus'])
  p.stop()
  assert.equal(timers.timerCount(), 0)
  assert.deepEqual(Object.keys(handlers), [])
  p.init()
  await flush()
  assert.equal(timers.timerCount(), 1)
  assert.deepEqual(Object.keys(handlers).sort(), ['blur', 'focus'])
})
