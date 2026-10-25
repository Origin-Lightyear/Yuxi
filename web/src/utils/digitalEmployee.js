export const DIGITAL_EMPLOYEE_STATUS = Object.freeze({
  draft: { label: '草稿', tone: 'warning' },
  active: { label: '在岗', tone: 'success' },
  disabled: { label: '已停用', tone: 'muted' }
})

export const EMPTY_DIGITAL_EMPLOYEE_PROFILE = Object.freeze({
  position: '',
  department: '',
  responsibilities: '',
  capabilities: [],
  boundaries: '',
  acceptance: '',
  status: 'draft',
  published_at: null,
  published_by: null,
  disabled_at: null,
  disabled_by: null
})

/** 统一员工身份字段并补齐档案默认值。 */
export function normalizeDigitalEmployee(agent) {
  const profile = agent?.digital_employee_profile || agent?.profile || {}
  const slug = agent?.slug || agent?.agent_id || agent?.id
  return {
    ...agent,
    id: agent?.id || slug,
    slug,
    kind: agent?.kind || 'digital_employee',
    profile: { ...EMPTY_DIGITAL_EMPLOYEE_PROFILE, ...profile }
  }
}

/** 返回员工状态展示文案，未知状态按草稿处理。 */
export function digitalEmployeeStatusMeta(status) {
  return DIGITAL_EMPLOYEE_STATUS[status] || DIGITAL_EMPLOYEE_STATUS.draft
}

/** 返回去空后的能力标签。 */
export function digitalEmployeeCapabilities(employee) {
  return Array.isArray(employee?.profile?.capabilities)
    ? employee.profile.capabilities.filter(Boolean)
    : []
}

/** 构造本地搜索文本，覆盖姓名、岗位、部门、职责和能力。 */
export function digitalEmployeeSearchText(employee) {
  const profile = employee?.profile || {}
  return [
    employee?.name,
    profile.position,
    profile.department,
    profile.responsibilities,
    ...(profile.capabilities || [])
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
}
