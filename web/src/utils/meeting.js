export const MEETING_STATUS_META = {
  draft: { label: '草稿', color: 'default' },
  awaiting_decisions: { label: '待拍板', color: 'warning' },
  effective: { label: '已生效', color: 'success' },
  rejected: { label: '已否决', color: 'error' },
}

export const DECISION_STATUS_META = {
  pending: { label: '待签字', color: 'warning' },
  effective: { label: '已生效', color: 'success' },
  rejected: { label: '已否决', color: 'error' }
}

export const SIGNER_STATUS_META = {
  pending: { label: '待签', color: 'default' },
  approved: { label: '已同意', color: 'success' },
  rejected: { label: '已拒绝', color: 'error' }
}

export function meetingStatusMeta(status) {
  return MEETING_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function decisionStatusMeta(status) {
  return DECISION_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function signerStatusMeta(status) {
  return SIGNER_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function speakerTypeLabel(type) {
  if (type === 'twin') return '分身'
  if (type === 'digital_employee') return '数字员工'
  if (type === 'human') return '人'
  return '系统'
}

export function meetingSpeakerName(message) {
  return message?.speaker_name || message?.speaker_slug || message?.speaker_uid || '未知发言人'
}

export function meetingCanRun(meeting) {
  return Boolean(meeting?.can_manage && meeting?.status === 'draft')
}

export function meetingCanJoin(meeting) {
  return Boolean(meeting?.can_join)
}
