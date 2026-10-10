import { scheduledAgentApi } from '@/apis/scheduled_agent_api'
import { notifyRunTerminal } from './desktopNotify.js'

/** 轮询间隔（毫秒）。 */
const POLL_INTERVAL_MS = 30000

/** 运行三终态：仅终态变化产生通知。 */
const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

/**
 * 构造定时任务失焦轮询器：仅桌面壳且窗口失焦时按 30s 拉取任务列表，
 * 以 run.id 状态快照差分出新终态并通知；首 tick 只建快照不通知。
 *
 * @param {{
 *   list: () => Promise<{ jobs: Array<{ name: string, runs: Array<object> }> }>,
 *   notify: (payload: { title?: string, threadId?: string, status: string }) => unknown,
 *   isDesktop?: () => boolean,
 *   hasFocus?: () => boolean,
 *   setTimer?: typeof setInterval,
 *   clearTimer?: typeof clearInterval
 * }} deps list/notify 为唯一外部副作用出口；时钟可注入以便测试不真实等待
 * @returns {{ init: () => void, stop: () => void }}
 */
export function createScheduledRunPoller({
  list,
  notify,
  isDesktop = () => !!globalThis.window?.yuxiDesktop,
  hasFocus = () => globalThis.document.hasFocus(),
  setTimer = setInterval,
  clearTimer = clearInterval
}) {
  // 快照跨 init/stop 保留：重启后旧终态不重发
  const snapshot = new Map()
  let timer = null
  let initialized = false
  let seeded = false

  const tick = async () => {
    const data = await list()
    for (const job of data.jobs) {
      for (const run of job.runs) {
        if (!seeded) {
          snapshot.set(run.id, run.status)
          continue
        }
        if (!TERMINAL.has(run.status) || snapshot.get(run.id) === run.status) continue
        snapshot.set(run.id, run.status)
        notify({ title: job.name, threadId: run.thread_id, status: run.status })
      }
    }
    seeded = true
  }

  const start = () => {
    if (timer) return
    timer = setTimer(() => {
      void tick()
    }, POLL_INTERVAL_MS)
    void tick()
  }

  const stopTimer = () => {
    if (!timer) return
    clearTimer(timer)
    timer = null
  }

  const handleBlur = () => start()
  const handleFocus = () => stopTimer()

  // 桌面壳判定在 init 处一次性闭合：非 Electron 环境零副作用
  const init = () => {
    if (initialized) return
    if (!isDesktop()) return
    initialized = true
    globalThis.window?.addEventListener?.('blur', handleBlur)
    globalThis.window?.addEventListener?.('focus', handleFocus)
    if (!hasFocus()) start()
  }

  const stop = () => {
    stopTimer()
    if (!initialized) return
    initialized = false
    globalThis.window?.removeEventListener?.('blur', handleBlur)
    globalThis.window?.removeEventListener?.('focus', handleFocus)
  }

  return { init, stop }
}

/** 生产单例：Task 5 的 App.vue 调 init() 启动。 */
export const scheduledRunPoller = createScheduledRunPoller({
  list: () => scheduledAgentApi.list(),
  notify: notifyRunTerminal
})
