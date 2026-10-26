export const FOUNDER_TWIN_STATUS_META = {
  draft: { label: '草稿', color: 'default' },
  confirmed: { label: '已确认', color: 'success' },
  invalid: { label: '已失效', color: 'error' }
}

export const HANDOFF_STATUS_META = {
  open: { label: '待老板回答', color: 'processing' },
  answered: { label: '已回答', color: 'success' },
  closed: { label: '已关闭', color: 'default' }
}

export function founderTwinEntryStatusMeta(status) {
  return FOUNDER_TWIN_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function founderTwinHandoffStatusMeta(status) {
  return HANDOFF_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function founderTwinCanManage(twin) {
  return Boolean(twin?.can_manage)
}
