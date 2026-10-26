export const WORK_ITEM_STATUS_META = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '执行中', color: 'processing' },
  waiting_approval: { label: '待审批', color: 'warning' },
  completed: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' },
  pending: { label: '排队中', color: 'default' },
  interrupted: { label: '已中断', color: 'error' }
}

export const WORK_ITEM_STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'waiting_approval', label: '待审批' },
  { value: 'running', label: '执行中' },
  { value: 'queued', label: '排队中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' }
]

export function workItemStatusLabel(status) {
  return WORK_ITEM_STATUS_META[status]?.label || '未知状态'
}

export function workItemStatusColor(status) {
  return WORK_ITEM_STATUS_META[status]?.color || 'default'
}

export function workItemSourceLabel(item) {
  if (item?.source_type === 'handoff') return '老板分身转交'
  if (item?.initiator_type === 'digital_employee') return '数字员工'
  return '人工发起'
}

export function workItemUnreadLabel(unread) {
  return unread ? '未读' : '已读'
}

export function workItemAction(item) {
  if (!item) return { key: 'none', label: '不可操作' }
  if (item.source_type === 'handoff') {
    return {
      key: 'open_handoff',
      label: item.status === 'completed' || item.status === 'cancelled' ? '查看转交' : '去处理转交'
    }
  }
  if (item.status === 'waiting_approval') {
    return { key: 'open_thread', label: '去对话处理' }
  }
  return { key: 'open_thread', label: '打开对话' }
}

export function workItemTarget(item) {
  if (item?.source_type === 'handoff') {
    return {
      name: 'FounderTwinComp',
      query: item.agent_slug ? { twin: item.agent_slug } : {}
    }
  }
  return {
    name: 'AgentCompWithThreadId',
    params: { thread_id: item?.thread_id }
  }
}
