import { useChatThreadsStore } from '@/stores/chatThreads'

/**
 * 运行三终态的系统通知文案。
 */
export const RUN_TERMINAL_TEXT = {
  completed: '已完成',
  failed: '执行失败',
  cancelled: '已取消'
}

/**
 * 解析通知标题：显式标题优先，其次按线程 ID 取会话标题，最终回退 'Yuxi'。
 *
 * Store 不可用（如 pinia 未激活）时静默回退，不影响通知本身。
 */
const resolveTitle = ({ title, threadId }) => {
  if (title) return title
  if (threadId) {
    try {
      const threadTitle = useChatThreadsStore().threads.find(
        (thread) => thread.id === threadId
      )?.title
      if (threadTitle) return threadTitle
    } catch {
      // Store 解析失败按默认标题回退
    }
  }
  return 'Yuxi'
}

/**
 * 构造一次运行终态系统通知。
 *
 * 仅 Electron 渲染进程（存在 window.yuxiDesktop）且窗口失焦时构造；
 * 其余环境在门控处返回 false，不触碰任何浏览器全局。
 * 点击通知时经桌面壳 show 通道恢复（可能被隐藏的）窗口并回焦，
 * 并在携带 threadId 时深链到对应会话。
 *
 * @param {{ threadId?: string, status?: string, title?: string }} [payload]
 * @returns {boolean} 是否构造了通知；任一门控失败返回 false
 */
export function notifyRunTerminal({ threadId, status, title } = {}) {
  if (!globalThis.window?.yuxiDesktop) return false
  if (globalThis.document.hasFocus()) return false
  const body = RUN_TERMINAL_TEXT[status]
  if (!body) return false

  const notification = new Notification(resolveTitle({ title, threadId }), { body })
  notification.onclick = () => {
    // 隐藏到托盘时 Electron 对 focus() 是 no-op，先经 preload 的 show 通道由主进程恢复窗口；
    // 非桌面或旧 preload 无该方法时 optional chaining 静默跳过
    globalThis.window.yuxiDesktop?.showMainWindow?.()
    globalThis.window.focus()
    if (threadId) {
      globalThis.window.location.assign(`/agent/${encodeURIComponent(threadId)}`)
    }
  }
  return true
}
