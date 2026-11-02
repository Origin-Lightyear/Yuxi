export const EMPLOYEE_SKILL_STATUS_META = {
  draft: { label: '草稿', color: 'default' },
  needs_review: { label: '待确认', color: 'warning' },
  ready: { label: '已就绪', color: 'success' },
  archived: { label: '已归档', color: 'default' }
}

export const SKILL_CAPTURE_METHOD_META = {
  interview: { label: '访谈', color: 'blue' },
  recording: { label: '录音', color: 'purple' },
  process: { label: '流程', color: 'cyan' },
  review: { label: '复盘', color: 'orange' }
}

export const HANDOVER_STATUS_META = {
  unassigned: { label: '未分配', color: 'default' },
  assigned: { label: '已指派', color: 'warning' },
  accepted: { label: '已接手', color: 'success' }
}

export function employeeSkillStatusMeta(status) {
  return EMPLOYEE_SKILL_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function captureMethodMeta(method) {
  return SKILL_CAPTURE_METHOD_META[method] || { label: method || '未分类', color: 'default' }
}

export function handoverStatusMeta(status) {
  return HANDOVER_STATUS_META[status] || { label: '未知状态', color: 'default' }
}

export function splitSkillLines(value) {
  return String(value || '')
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}
