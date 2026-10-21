// web/test/unit/connectionMonitor.test.js
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { createServer } from 'vite'

// monitor 静态 import 链（system_api → base → stores/user）在模块求值时读取 localStorage（与 scheduledRunPoller.test.js 相同）
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {}
}

// 惰性单例：与 scheduledRunPoller.test.js 相同的 ssrLoadModule 模式（模块级 promise，无 before 钩子）
let modPromise
let server
const loadMod = () =>
  (modPromise ??= createServer({ server: { middlewareMode: true }, appType: 'custom' }).then(async (s) => {
    server = s
    return s.ssrLoadModule('/src/utils/connectionMonitor.js')
  }))

// middleware 模式的 vite server 仍持有 watcher 句柄，不关闭则测试进程无法退出
after(() => server?.close())

const flush = () => setImmediate()

test('探测失败进入 offline，短断连恢复不刷新', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  let fail = true
  let reloaded = 0
  let clock = 0
  const { createConnectionMonitor } = await loadMod()
  const m = createConnectionMonitor({
    check: async () => {
      if (fail) throw new Error('down')
      return true
    },
    reload: () => {
      reloaded += 1
    },
    now: () => clock
  })
  const seen = []
  m.subscribe((s) => seen.push(s))
  assert.deepEqual(seen, ['online']) // 订阅立即回调当前值
  m.start()
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(m.state(), 'offline')
  assert.deepEqual(seen, ['online', 'offline'])
  fail = false
  clock = 25000 // 断连 25s < 30s
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(m.state(), 'online')
  assert.equal(reloaded, 0)
})

test('断连 ≥30s 恢复触发刷新', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  let fail = true
  let reloaded = 0
  let clock = 0
  const { createConnectionMonitor } = await loadMod()
  const m = createConnectionMonitor({
    check: async () => {
      if (fail) throw new Error('down')
      return true
    },
    reload: () => {
      reloaded += 1
    },
    now: () => clock
  })
  m.start()
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(m.state(), 'offline')
  fail = false
  clock = 40000 // 断连 40s ≥ 30s
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(reloaded, 1)
})

test('持续失败保持 offline 且 since 不重置', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  let fail = true
  let reloaded = 0
  let clock = 0
  const { createConnectionMonitor } = await loadMod()
  const m = createConnectionMonitor({
    check: async () => {
      if (fail) throw new Error('down')
      return true
    },
    reload: () => {
      reloaded += 1
    },
    now: () => clock
  })
  m.start()
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(m.state(), 'offline') // 首败 clock=0 记 since
  clock = 10000
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(m.state(), 'offline') // 重复失败不重置 since
  fail = false
  clock = 35000 // 若 since 仍为首次失败 0：35s ≥ 30s → 刷新；若被重置为 10000：25s < 30s → 不刷新
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(reloaded, 1)
})

test('健康持续成功保持 online 且不刷新', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  let reloaded = 0
  const { createConnectionMonitor } = await loadMod()
  const m = createConnectionMonitor({
    check: async () => true,
    reload: () => {
      reloaded += 1
    },
    now: () => 0
  })
  m.start()
  t.mock.timers.tick(20000)
  t.mock.timers.tick(20000)
  await flush()
  assert.equal(m.state(), 'online')
  assert.equal(reloaded, 0)
})
