# 桌面壳 v1：运行通知与客户端体验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Electron 桌面壳交付运行终态通知（当前会话 + 定时任务）、托盘常驻、窗口状态、原生菜单、自启、断连检测与三项安全/集成小修，浏览器端行为零变化。

**Architecture:** 一切新能力以 `window.yuxiDesktop`（preload 是否注入）为门控；通知/轮询/断连检测全部在渲染层（HTML5 Notification + 定时轮询），`preload.js` 零改动；`main.js` 承担托盘、菜单、自启、窗口状态与两个协议边界，纯逻辑抽成独立模块以便 `node --test`。

**Tech Stack:** Electron 37（主进程 CJS）、Vue 3 渲染层（ESM）、Node 内置 `node --test`（desktop 与 web 共用 runner）、vite `ssrLoadModule`（web 单测加载 `@/` 别名模块）。

**Spec:** `docs/develop-guides/decisions/proposed/2026-10-10-desktop-shell-v1.md`

## Global Constraints

- Windows 是目标运行平台（NSIS 安装版，appId `com.yuxi.desktop`）；macOS 仅开发与测试平台。
- 浏览器打开 web 行为零变化：所有新能力以 `window.yuxiDesktop` 存在性门控；`preload.js` 零改动。
- 精确值：定时轮询 30000ms（仅桌面壳且失焦）；健康探测 20000ms；断连 <30000ms 恢复不刷新、≥30000ms 恢复自动刷新；窗口默认 1280×820、最小 900×600；move/resize 防抖 500ms。
- 通知只覆盖 `completed`/`failed`/`cancelled` 三终态（`interrupted` 不通知）；文案固定：已完成 / 执行失败 / 已取消；通知与菜单文案为中文。
- `app.isPackaged === false`（开发直跑）时开机自启 checkbox 禁用。
- `openExternal` 白名单仅 `http:`/`https:`/`mailto:`；will-download `setSavePath` 到 `app.getPath('downloads')`，重名编号，不弹保存对话框。
- `desktop/electron-builder.yml` 的 `files` 是白名单：新增的 `desktop/*.js` 模块必须显式加入，否则打包缺失。
- web 单测命令 `docker compose exec web pnpm run test:unit`（node --test，`test/**/*.test.js`）；lint gate `docker compose exec web pnpm run lint:check`；desktop 单测 `node --test "desktop/test/*.test.js"`。
- 每任务一次中文 Conventional Commit（含 why body）。

## Review Focus

1. 托盘隐藏态下 Chromium 后台节流使 30s 轮询失速、定时通知迟到或不发 —— Task 10 的隐藏态 E2E 行钉住；若失败，检查 Task 6 的两个 commandLine switch 是否在 `app.whenReady` 之前生效。
2. 通知 deep-link 拿不到 threadId 或 status 不在三终态内 —— Task 3 的负向 unit：无 threadId 只回焦不跳转、未知 status 不构造 Notification。
3. 轮询器首 tick 风暴（启动即补发历史终态通知）与 focus/blur 竞态 —— Task 4 的 unit：seed tick 只建快照不通知、初始未失焦不建定时器、focus 清定时器且旧快照不重发。
4. 关闭到托盘与启动连接循环的 `app.quit()` 路径交互（用户在连接弹窗点「退出」必须真退出）—— Task 6 的 E2E 行钉住 `isQuitting` 覆盖所有退出路径。
5. 白名单与既有 `target.startsWith(url)` 同源判断叠加后同源导航被误伤、`file:` 拖放外开 —— Task 1 unit（同源 http 放行、file: 拒绝）+ Task 10 拖放 E2E。

---

### Task 1: URL 外开白名单

**Files:**
- Create: `desktop/url_policy.js`
- Create: `desktop/test/url_policy.test.js`
- Modify: `desktop/main.js:162-172`（`setWindowOpenHandler` 与 `will-navigate` 两个调用点）
- Modify: `desktop/electron-builder.yml:7-10`（`files` 增加 `- url_policy.js`）

**Interfaces:**
- Consumes: 现有 `shell.openExternal` 两处调用。
- Produces: `isAllowedExternalUrl(raw: string) → boolean`（CJS 导出），Task 10 的拖放 E2E 依赖此行为。

- [ ] **Step 1: 写失败测试**

```js
// desktop/test/url_policy.test.js
const test = require('node:test')
const assert = require('node:assert/strict')
const { isAllowedExternalUrl } = require('../url_policy.js')

test('放行 http/https/mailto', () => {
  assert.equal(isAllowedExternalUrl('https://example.com/a?b=1'), true)
  assert.equal(isAllowedExternalUrl('http://example.com'), true)
  assert.equal(isAllowedExternalUrl('mailto:a@b.com'), true)
})
test('拒绝危险与本地协议', () => {
  assert.equal(isAllowedExternalUrl('javascript:alert(1)'), false)
  assert.equal(isAllowedExternalUrl('file:///etc/passwd'), false)
  assert.equal(isAllowedExternalUrl('data:text/html,x'), false)
  assert.equal(isAllowedExternalUrl('not a url'), false)
  assert.equal(isAllowedExternalUrl(''), false)
})
test('大小写与首尾空白不影响判定', () => {
  assert.equal(isAllowedExternalUrl('  HTTPS://example.com  '), true)
  assert.equal(isAllowedExternalUrl('JavaScript:alert(1)'), false)
})
```

- [ ] **Step 2: 运行确认失败**

Run: `node --test desktop/test/url_policy.test.js`
Expected: FAIL（`Cannot find module '../url_policy.js'`）

- [ ] **Step 3: 实现 `isAllowedExternalUrl(raw: string): boolean`**

`desktop/url_policy.js`（CJS）：`trim` 后 `new URL`（抛错返回 false），判定 `protocol` 是否在 `['http:', 'https:', 'mailto:']`。

- [ ] **Step 4: 运行确认通过**

Run: `node --test desktop/test/url_policy.test.js`
Expected: PASS（3 tests）

- [ ] **Step 5: 接线两个调用点 + builder 白名单**

`main.js` 两处 `shell.openExternal(target)` 前加 `if (!isAllowedExternalUrl(target)) return`（`setWindowOpenHandler` 内 return `{action:'deny'}`，`will-navigate` 内仅 preventDefault）。`electron-builder.yml` `files` 加 `- url_policy.js`。

- [ ] **Step 6: 回归 + 提交**

Run: `node --test "desktop/test/*.test.js"`
Expected: PASS

```bash
git add desktop/url_policy.js desktop/test/url_policy.test.js desktop/main.js desktop/electron-builder.yml
git commit -m "feat(desktop): 外开链接增加 http/https/mailto 白名单" -m "防止页面注入或拖放触发任意协议外开，file: 拖放不再交系统打开。"
```

---

### Task 2: 窗口状态记忆

**Files:**
- Create: `desktop/window_state.js`
- Create: `desktop/test/window_state.test.js`
- Modify: `desktop/main.js`（`createWindow`、close/move/resize）
- Modify: `desktop/electron-builder.yml`（`files` 增加 `- window_state.js`）

**Interfaces:**
- Consumes: 既有 `loadConfig()`/`saveConfig()`（`main.js:30-42`，`windowState` 为新增键）。
- Produces: `sanitizeWindowState(state, displays) → {x,y,width,height,maximized} | null`（CJS）；`displays` 形参为 `{x,y,width,height}[]`（取 `screen.getAllDisplays().map(d => d.workArea)`）。

- [ ] **Step 1: 写失败测试**

```js
// desktop/test/window_state.test.js
const test = require('node:test')
const assert = require('node:assert/strict')
const { sanitizeWindowState } = require('../window_state.js')

const oneDisplay = [{ x: 0, y: 0, width: 1920, height: 1080 }]

test('合法状态原样返回并归一 maximized', () => {
  assert.deepEqual(
    sanitizeWindowState({ x: 100, y: 50, width: 1280, height: 820, maximized: true }, oneDisplay),
    { x: 100, y: 50, width: 1280, height: 820, maximized: true }
  )
})
test('落在显示器外返回 null', () => {
  assert.equal(sanitizeWindowState({ x: 99999, y: 99999, width: 1280, height: 820 }, oneDisplay), null)
})
test('字段缺失或低于最小尺寸返回 null', () => {
  assert.equal(sanitizeWindowState({ x: 10, y: 10, width: 800, height: 600 }, oneDisplay), null)
  assert.equal(sanitizeWindowState({ x: 10, y: 10, width: 1280 }, oneDisplay), null)
  assert.equal(sanitizeWindowState(null, oneDisplay), null)
})
test('多显示器任一相交即合法', () => {
  const two = [{ x: 0, y: 0, width: 1920, height: 1080 }, { x: 1920, y: 0, width: 1920, height: 1080 }]
  assert.notEqual(sanitizeWindowState({ x: 2000, y: 100, width: 1280, height: 820 }, two), null)
})
```

- [ ] **Step 2: 运行确认失败**

Run: `node --test desktop/test/window_state.test.js`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `sanitizeWindowState(state, displays)`**

CJS。校验 `x/y/width/height` 为有限数、`width≥900 && height≥600`、矩形与任一 display 有交集；通过返回 `{x,y,width,height,maximized: !!state.maximized}`，否则 `null`。

- [ ] **Step 4: 运行确认通过**

Run: `node --test desktop/test/window_state.test.js`
Expected: PASS（4 tests）

- [ ] **Step 5: 接线 `main.js`**

- `createWindow`：读 `loadConfig().windowState`，经 `sanitizeWindowState(state, screen.getAllDisplays().map(d => d.workArea))` 校验，`null` 则用默认 1280×820；作为 BrowserWindow 初始 bounds，`maximized: true` 时 `maximize()`。
- 保存：`move`/`resize` 防抖 500ms（模块级 timer）写 `saveConfig({ windowState })`；`close` 时立即保存，最大化用 `getNormalBounds()` 的 bounds + `maximized: mainWindow.isMaximized()`。
- `electron-builder.yml` `files` 加 `- window_state.js`。

- [ ] **Step 6: 回归 + 提交**

Run: `node --test "desktop/test/*.test.js"`
Expected: PASS

```bash
git add desktop/window_state.js desktop/test/window_state.test.js desktop/main.js desktop/electron-builder.yml
git commit -m "feat(desktop): 记忆窗口位置与大小并校验显示器边界" -m "重启恢复上次布局；显示器拔除后超界状态回默认，避免窗口不可见。"
```

---

### Task 3: 终态通知 util + 会话流挂点

**Files:**
- Create: `web/src/utils/desktopNotify.js`
- Create: `web/test/unit/desktopNotify.test.js`
- Modify: `web/src/composables/useAgentRunStream.js:403-412`（`event === 'end'` 的终态分支）

**Interfaces:**
- Consumes: `useChatThreadsStore`（`web/src/stores/chatThreads.js:18`，`threads` ref 数组，元素含 `id`/`title`）；`useAgentRunStream.js` 终态分支已有 `threadId`、`terminalStatus`。
- Produces: `notifyRunTerminal({ threadId, status, title } = {}) → boolean`（构造并返回 true；任一门控失败返回 false）。Task 4 的 poller 以同一函数为通知出口。

- [ ] **Step 1: 写失败测试**

```js
// web/test/unit/desktopNotify.test.js
import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

// 惰性单例：与 agentRequestQueue.test.js 相同的 ssrLoadModule 模式（模块级 promise，无 before 钩子）
let modPromise
const loadMod = () =>
  (modPromise ??= createServer({ server: { middlewareMode: true }, appType: 'custom' }).then(async (server) => {
    const mod = await server.ssrLoadModule('/src/utils/desktopNotify.js')
    return mod
  }))

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
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec web pnpm run test:unit`
Expected: FAIL（desktopNotify 模块不存在）

- [ ] **Step 3: 实现 `web/src/utils/desktopNotify.js`**

导出 `RUN_TERMINAL_TEXT = { completed:'已完成', failed:'执行失败', cancelled:'已取消' }` 与 `notifyRunTerminal({ threadId, status, title } = {}) → boolean`。顺序门控：无 `window.yuxiDesktop` → false；`document.hasFocus()` → true → false；`RUN_TERMINAL_TEXT[status]` 缺失 → false。标题：`title` 缺省且有 `threadId` 时从 `useChatThreadsStore().threads` 按 id 取 `title`，再缺省 `'Yuxi'`。`new Notification(title, { body })`，`onclick` → `window.focus()`；有 `threadId` 时 `window.location.assign('/agent/' + encodeURIComponent(threadId))`。

- [ ] **Step 4: 运行确认通过**

Run: `docker compose exec web pnpm run test:unit`
Expected: PASS

- [ ] **Step 5: 挂入会话流终态分支**

`useAgentRunStream.js` 的 `if (event === 'end')` 且 `RUN_TERMINAL_STATUSES.has(terminalStatus)` 分支内、`finalizeRunStream` 前调用 `notifyRunTerminal({ threadId, status: terminalStatus })`（模块顶部 import）。

- [ ] **Step 6: 回归 + 提交**

Run: `docker compose exec web pnpm run lint:check && docker compose exec web pnpm run test:unit`
Expected: 均通过

```bash
git add web/src/utils/desktopNotify.js web/test/unit/desktopNotify.test.js web/src/composables/useAgentRunStream.js
git commit -m "feat(web): 桌面壳运行终态系统通知" -m "失焦时弹系统通知并直达会话；浏览器环境以 yuxiDesktop 门控保持零变化。"
```

---

### Task 4: 定时任务轮询器

**Files:**
- Create: `web/src/utils/scheduledRunPoller.js`
- Create: `web/test/unit/scheduledRunPoller.test.js`

**Interfaces:**
- Consumes: `scheduledAgentApi.list()` → `{ jobs: [{ name, runs: [{ id, thread_id, status }] }] }`（`web/src/apis/scheduled_agent_api.js:5`、`scheduled_agent_service.py:163-176`）；`notifyRunTerminal`（Task 3）。
- Produces: `createScheduledRunPoller(deps) → { init(), stop() }`；生产单例 `scheduledRunPoller`（deps 固定为 `scheduledAgentApi.list` + `notifyRunTerminal`）。Task 5 的 App.vue 调 `scheduledRunPoller.init()`。

- [ ] **Step 1: 写失败测试**

```js
// web/test/unit/scheduledRunPoller.test.js
import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'

let modPromise
const loadMod = () =>
  (modPromise ??= createServer({ server: { middlewareMode: true }, appType: 'custom' }).then((s) =>
    s.ssrLoadModule('/src/utils/scheduledRunPoller.js')
  ))

const job = (runs) => ({ jobs: [{ name: '月度经营分析', runs }] })

test('非桌面或初始聚焦时不启动定时器', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  globalThis.window = {}
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const p = createScheduledRunPoller({ list: async () => job([]), notify: () => {} })
  p.init()
  assert.equal(t.mock.timers.timerCount, 0)
})

test('首 tick 只建快照不通知，后续新终态才通知', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  globalThis.window = { yuxiDesktop: {} }
  globalThis.document = { hasFocus: () => false }
  const { createScheduledRunPoller } = await loadMod()
  const notified = []
  let payload = job([{ id: 'r1', thread_id: 't1', status: 'running' }])
  const p = createScheduledRunPoller({ list: async () => payload, notify: (o) => notified.push(o) })
  p.init() // 初始失焦 → 立即 tick（seed）
  await new Promise((r) => setImmediate(r))
  t.mock.timers.tick(30000)
  await new Promise((r) => setImmediate(r))
  assert.equal(notified.length, 0)
  payload = job([{ id: 'r1', thread_id: 't1', status: 'completed' }])
  t.mock.timers.tick(30000)
  await new Promise((r) => setImmediate(r))
  assert.equal(notified.length, 1)
  assert.deepEqual(notified[0], { title: '月度经营分析', threadId: 't1', status: 'completed' })
})

test('聚焦停止定时器且旧终态不重发', async (t) => { /* focus 后 timerCount=0；回失焦后 tick 不重复通知 r1 */ })
test('非三终态变化不通知', async (t) => { /* running→interrupted→running 不产生通知 */ })
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec web pnpm run test:unit`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `web/src/utils/scheduledRunPoller.js`**

`POLL_INTERVAL_MS = 30000`；`TERMINAL = new Set(['completed','failed','cancelled'])`。`createScheduledRunPoller({ list, notify, isDesktop = () => !!globalThis.window?.yuxiDesktop, hasFocus = () => globalThis.document.hasFocus() })`：模块级 `snapshot = new Map()`（跨 init/stop 保留）、`timer`、`initialized`。`init()`：幂等注册 `window` 的 `blur`/`focus` 监听；`blur` → 未在跑则建 30000ms 定时器并立即 `tick()`；`focus` → 清定时器。`tick()`：`list()` → 遍历 `jobs[].runs[]`；`TERMINAL.has(status)` 且 `snapshot.get(id) !== status` 时 `notify({ title: job.name, threadId: run.thread_id, status })`；**首 tick 之前 `seeded === false` 时只写快照不通知**。文件末尾导出生产单例 `scheduledRunPoller = createScheduledRunPoller({ list: () => scheduledAgentApi.list(), notify: notifyRunTerminal })`（相对/别名 import 与 web 源码风格一致，测试走 ssrLoadModule 解析）。

- [ ] **Step 4: 运行确认通过**

Run: `docker compose exec web pnpm run test:unit`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/src/utils/scheduledRunPoller.js web/test/unit/scheduledRunPoller.test.js
git commit -m "feat(web): 定时任务失焦轮询终态通知" -m "后端无用户级事件通道，30s 轮询每任务最近 3 条 run 快照差分出新终态；仅桌面壳且失焦运行。"
```

---

### Task 5: 断连监控 + 横幅 + App 接线

**Files:**
- Create: `web/src/utils/connectionMonitor.js`
- Create: `web/src/components/ConnectionStatusBanner.vue`
- Create: `web/test/unit/connectionMonitor.test.js`
- Modify: `web/src/App.vue:6-18`（`onMounted` 接线 + 模板挂横幅）

**Interfaces:**
- Consumes: `healthApi.checkHealth()`（`web/src/apis/system_api.js:12`）；Task 4 的 `scheduledRunPoller.init()`。
- Produces: `createConnectionMonitor({ check, reload, now }) → { start(), stop(), subscribe(cb) → unsubscribe, state() }`；生产单例 `connectionMonitor`；`ConnectionStatusBanner.vue`（离线固定横幅，桌面门控内部自检）。

- [ ] **Step 1: 写失败测试**

```js
// web/test/unit/connectionMonitor.test.js
import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'vite'

let modPromise
const loadMod = () =>
  (modPromise ??= createServer({ server: { middlewareMode: true }, appType: 'custom' }).then((s) =>
    s.ssrLoadModule('/src/utils/connectionMonitor.js')
  ))

test('探测失败进入 offline，短断连恢复不刷新', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] })
  let fail = true, reloaded = 0, clock = 0
  const { createConnectionMonitor } = await loadMod()
  const m = createConnectionMonitor({
    check: async () => { if (fail) throw new Error('down'); return true },
    reload: () => { reloaded += 1 },
    now: () => clock,
  })
  m.start()
  t.mock.timers.tick(20000); await new Promise((r) => setImmediate(r))
  assert.equal(m.state(), 'offline')
  fail = false; clock = 25000 // 断连 25s < 30s
  t.mock.timers.tick(20000); await new Promise((r) => setImmediate(r))
  assert.equal(m.state(), 'online'); assert.equal(reloaded, 0)
})
test('断连 ≥30s 恢复触发刷新', async (t) => { /* clock=40000 → reload===1 */ })
test('持续失败保持 offline 且 since 不重置', async (t) => { /* 两次失败后恢复 clock 仍按首次失败计 */ })
```

- [ ] **Step 2: 运行确认失败**

Run: `docker compose exec web pnpm run test:unit`
Expected: FAIL

- [ ] **Step 3: 实现 `connectionMonitor.js`**

`PROBE_INTERVAL_MS = 20000`、`RELOAD_THRESHOLD_MS = 30000`。`createConnectionMonitor({ check, reload, now })`：`state` 初始 `online`；每 20s `check()`——失败：首败记 `offlineSince = now()`、状态转 `offline`（重复失败不重置 since）；成功且此前 offline：`now() - offlineSince >= 30000` → `reload()`，否则状态回 `online`。`subscribe(cb)` 收状态变化立即回调当前值。生产单例 deps：`check: () => healthApi.checkHealth()`、`reload: () => globalThis.window.location.reload()`。

- [ ] **Step 4: 实现 `ConnectionStatusBanner.vue` + App 接线**

横幅：`setup` 内 `if (!globalThis.window?.yuxiDesktop) return`（不订阅、渲染 null）；订阅 state，`offline` 时渲染固定顶部条「连接已断开，正在重试…」（中文，`role="alert"`）。`App.vue` `onMounted`：`globalThis.window?.yuxiDesktop` 时 `scheduledRunPoller.init()` 与 `connectionMonitor.start()`；模板顶部挂 `<ConnectionStatusBanner />`。

- [ ] **Step 5: 运行确认通过 + 提交**

Run: `docker compose exec web pnpm run lint:check && docker compose exec web pnpm run test:unit`
Expected: 均通过

```bash
git add web/src/utils/connectionMonitor.js web/src/components/ConnectionStatusBanner.vue web/test/unit/connectionMonitor.test.js web/src/App.vue
git commit -m "feat(web): 桌面壳断连检测横幅与全局接线" -m "后端断连时页面静默变死；20s 探测给横幅，长断连恢复自动刷新重建数据一致性。"
```

---

### Task 6: 保活 switches + 托盘 + 关闭到托盘

**Files:**
- Modify: `desktop/main.js`（switches、Tray、close/quit、second-instance、首 toast）

**Interfaces:**
- Consumes: Task 1/2 的模块与既有 `loadConfig`/`saveConfig`、`resolveAppIcon()`（`main.js:49`）。
- Produces: 托盘菜单为 Task 7（自启 checkbox）与 Task 9（切换地址）提供菜单模板拼装位置；`isQuitting` 标志为所有退出路径共享。

- [ ] **Step 1: 实现**

- 模块顶部（`require` 后立即）：`app.commandLine.appendSwitch('disable-renderer-backgrounding')`、`app.commandLine.appendSwitch('disable-backgrounding-occluded-windows')`。
- `let isQuitting = false`；`app.on('before-quit', () => { isQuitting = true })`。
- `mainWindow.on('close', e => { if (isQuitting) return; e.preventDefault(); 保存 windowState（Task 2 的 close 保存逻辑）; mainWindow.hide(); firstTrayHint(); })`。
- `firstTrayHint()`：`loadConfig().trayHintShown` 为真则返回；否则 `new Notification({ title: '大力水手', body: '已最小化到托盘，双击托盘图标可恢复' })` 并 `saveConfig({ trayHintShown: true })`（electron 主进程 `Notification` 从 `electron` 导入）。
- `new Tray(resolveAppIcon())`（`whenReady` 内，图标 null 时跳过并记 `console.warn`）；托盘模板：`显示主窗口`（恢复并聚焦）、`切换服务地址`（此任务先放占位分隔）、`开机自启动`（Task 7 填充）、`退出`（`isQuitting = true; app.quit()`）。左键单击 = 显示主窗口。
- `second-instance`：在既有 restore/focus 前加 `if (mainWindow && mainWindow.isHidden()) mainWindow.show()`。
- `window-all-closed` 保持现状（close 被 preventDefault 后不触发，仅作兑底）。

- [ ] **Step 2: 手工验证（macOS 开发机）**

Run: `cd desktop && npm start`
Checks: 关闭窗口 → 进程仍在、托盘图标在、首次出 toast、再次关闭不再出；托盘「显示主窗口」恢复；「退出」进程结束；`npm start` 下通知若不出现，在 `app.whenReady` 前补 `app.setAppUserModelId('com.yuxi.desktop')` 一行后复测并把结论写进提交说明。

- [ ] **Step 3: 提交**

```bash
git add desktop/main.js
git commit -m "feat(desktop): 关闭到托盘常驻并禁止后台节流" -m "托盘常驻是定时任务通知的前提；两个 commandLine switch 保证隐藏窗口内 30s 轮询不失速。"
```

---

### Task 7: 开机自启 checkbox

**Files:**
- Modify: `desktop/main.js`（托盘菜单「开机自启动」项）

**Interfaces:**
- Consumes: Task 6 的托盘菜单模板。
- Produces: 无（系统登录项为事实源，不写 config）。

- [ ] **Step 1: 实现**

菜单项 `开机自启动`：`type: 'checkbox'`，`checked: app.getLoginItemSettings().openAtLogin`；`enabled: app.isPackaged`；`click: (menuItem) => app.setLoginItemSettings({ openAtLogin: menuItem.checked, path: process.execPath })`。

- [ ] **Step 2: 手工验证（Windows 安装版优先，mac 开发机次之）**

Checks: 勾选 → 系统登录项出现本应用（Windows：任务管理器「启动应用」；mac：系统设置「通用→登录项」）；取消 → 移除；`npm start` 直跑时该项置灰不可点。

- [ ] **Step 3: 提交**

```bash
git add desktop/main.js
git commit -m "feat(desktop): 托盘菜单提供开机自启开关" -m "系统登录项为唯一事实源，避免应用与系统状态双写不一致。"
```

---

### Task 8: 原生菜单与右键编辑菜单

**Files:**
- Modify: `desktop/main.js`（`Menu.setApplicationMenu`、`context-menu`）

**Interfaces:**
- Consumes: 无。
- Produces: 无。

- [ ] **Step 1: 实现**

- `const { Menu } = require('electron')`；`whenReady` 内 `Menu.setApplicationMenu(buildMenu())`。
- `buildMenu()`：通用模板 `{ label: '编辑', submenu: [撤销/重做/剪切/复制/粘贴/全选 roles + 分隔] }`、`{ label: '查看', submenu: [reload role] }`、`{ label: '应用', submenu: [{ role: 'quit', label: '退出' }] }`；`process.platform === 'darwin'` 时前置 app 菜单（`about`/`hide`/`quit`，中文 label）。
- `mainWindow.webContents.on('context-menu', (e, params) => { if (!params.isEditable) return; Menu.getApplicationMenu() 的编辑 submenu → menu.popup({ window: mainWindow }) })`（构造独立 popup 菜单引用，避免依赖全局模板结构）。

- [ ] **Step 2: 手工验证**

Checks: 输入框内 Ctrl/Cmd+C、V、A 生效；右键弹出撤销/剪切/复制/粘贴/全选；非编辑区右键无菜单。

- [ ] **Step 3: 提交**

```bash
git add desktop/main.js
git commit -m "feat(desktop): 原生菜单与可编辑区右键菜单" -m "Electron 壳默认无编辑菜单与右键菜单，复制粘贴快捷键不可用是常见基础体验缺口。"
```

---

### Task 9: will-download 处理 + 托盘切换服务地址

**Files:**
- Modify: `desktop/main.js`（`will-download`、抽取 `ensureBackend` helper、托盘「切换服务地址」）

**Interfaces:**
- Consumes: 既有启动连接循环（`main.js:220-249` 的 `waitForBackend` + 三选弹窗）与 `askUrl()`。
- Produces: `ensureBackend(currentUrl) → Promise<string>`（成功返回可用 url；用户选退出时返回 `null` 且调用方应 `app.quit()`）——启动循环与托盘切换共用。

- [ ] **Step 1: 实现**

- 抽取：把启动循环的「`waitForBackend` 失败 → 三选弹窗 → 改地址/重试/退出」体抽成 `async function ensureBackend(currentUrl)`；启动循环改为 `backendUrl = await ensureBackend(backendUrl) ?? (app.quit(), return)` 语义（保持现有行为逐字等价）。
- 托盘「切换服务地址」：`const url = await askUrl(backendUrl)`；有值则 `const ok = await ensureBackend(url)`，成功 `saveConfig({ backendUrl: ok }); backendUrl = ok; mainWindow.loadURL(ok + '/agent')`；`ensureBackend` 返回 null（用户点退出）→ `isQuitting = true; app.quit()`。
- `session.defaultSession.on('will-download', (_e, item) => { let p = path.join(app.getPath('downloads'), item.getFilename()); let i = 1; while (fs.existsSync(p)) { p = path.join(app.getPath('downloads'), `${name} (${i})${ext}`); i += 1 } item.setSavePath(p) })`（重名编号，不弹对话框）。

- [ ] **Step 2: 手工验证**

Checks: 托盘切到可达地址 → 页面重载；切到不可达 → 出现「修改地址/重试/退出」弹窗且「退出」真退出；触发一次文件导出 → 落系统下载目录、同名再下一次出现 `(1)` 后缀；启动连接失败路径行为与改前一致（回归）。

- [ ] **Step 3: 提交**

```bash
git add desktop/main.js
git commit -m "feat(desktop): 托盘切换服务地址与下载落盘处理" -m "正常使用中切换内网/公网地址此前无入口；下载行为显式化避免依赖 Electron 默认且重名不覆盖。"
```

---

### Task 10: 拖拽验证 + 全量验收

**Files:**
- 无新增代码（预期拖拽区内零改动；发现 Electron 特有问题时最小修复并回写本任务说明）

**Interfaces:**
- Consumes: Task 1-9 全部行为。
- Produces: spec 验收矩阵的实测结果（回填 `docs/develop-guides/decisions/proposed/2026-10-10-desktop-shell-v1.md` 验证段，记录 → implemented 由 Reviewer 流程处理）。

- [ ] **Step 1: Windows NSIS 安装版 E2E 清单**

按 spec 验收矩阵逐行执行并记录结果：
1. 失焦终态通知 + 点击直达 `/agent/:thread_id`
2. run-now 定时任务失焦通知（含任务名）
3. **关闭到托盘后再 run-now → 通知仍按 30s 附近到达**（switches 生效验证）
4. 窗口状态移动后重启恢复；改分辨率制造超界回默认
5. 关闭→托盘→托盘退出→任务管理器无残留进程
6. 自启勾选/取消 ↔ 系统登录项一致
7. 菜单快捷键 + 右键编辑菜单
8. 附件区拖拽上传可用；区外拖 `file:` 不再外开
9. 下载落盘 + 重名编号
10. 杀后端 → 横幅；<30s 恢复不刷新、≥30s 恢复自动刷新
11. 托盘切地址：可达/不可达两路径
12. 开发直跑通知形态（toast 或降级）记录结论

- [ ] **Step 2: 全量命令**

```bash
node --test desktop/test/
docker compose exec web pnpm run lint:check
docker compose exec web pnpm run test:unit
docker compose exec web pnpm run build
python3 scripts/verify_engineering_contracts.py
```
Expected: 全部通过；docs 变更另跑 `cd docs && npm run build`。

- [ ] **Step 3: 回填决策记录 + 提交**

把 E2E 实测结论（含开发直跑通知形态、拖拽结论）写入决策记录验证段。

```bash
git add docs/develop-guides/decisions/proposed/2026-10-10-desktop-shell-v1.md
git commit -m "docs(desktop): 回填桌面壳 v1 验收实测结果" -m "验收矩阵要求实测证据，未验证项不得写成通过。"
```
