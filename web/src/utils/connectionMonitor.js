import { healthApi } from '@/apis/system_api'

/** 健康探测间隔（毫秒）。 */
const PROBE_INTERVAL_MS = 20000

/** 断连时长达到该阈值时，恢复成功后自动刷新页面。 */
const RELOAD_THRESHOLD_MS = 30000

/**
 * 构造后端连通性监控：每 20s 探测一次健康接口，
 * 失败转 offline（首败记时间点，重复失败不重置）；
 * 此前 offline 且探测成功时，断连 ≥30s 调用 reload，否则回 online。
 *
 * @param {{
 *   check: () => Promise<unknown>,
 *   reload: () => unknown,
 *   now?: () => number
 * }} deps check/reload 为唯一外部副作用出口；now 可注入以便测试不真实等待
 * @returns {{
 *   start: () => void,
 *   stop: () => void,
 *   subscribe: (cb: (state: 'online' | 'offline') => void) => () => void,
 *   state: () => 'online' | 'offline'
 * }}
 */
export function createConnectionMonitor({ check, reload, now = () => Date.now() }) {
  let status = 'online'
  let offlineSince = null
  let timer = null
  const listeners = new Set()

  const setState = (next) => {
    if (status === next) return
    status = next
    for (const cb of listeners) cb(next)
  }

  const tick = async () => {
    try {
      await check()
      if (status !== 'offline') return
      if (now() - offlineSince >= RELOAD_THRESHOLD_MS) {
        reload()
        return
      }
      offlineSince = null
      setState('online')
    } catch {
      if (status === 'offline') return
      offlineSince = now()
      setState('offline')
    }
  }

  const start = () => {
    if (timer) return
    timer = setInterval(() => {
      void tick()
    }, PROBE_INTERVAL_MS)
  }

  const stop = () => {
    if (!timer) return
    clearInterval(timer)
    timer = null
  }

  /** 订阅状态变化：注册后立即回调当前值，返回取消订阅函数。 */
  const subscribe = (cb) => {
    listeners.add(cb)
    cb(status)
    return () => listeners.delete(cb)
  }

  const state = () => status

  return { start, stop, subscribe, state }
}

/** 生产单例：App.vue 在桌面壳门控内调 start() 启动。 */
export const connectionMonitor = createConnectionMonitor({
  check: () => healthApi.checkHealth(),
  reload: () => globalThis.window.location.reload()
})
