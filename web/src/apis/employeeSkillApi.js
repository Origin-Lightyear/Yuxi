import { apiDelete, apiGet, apiPost, apiPut, buildQuery } from './base'

const skillPath = (id) => `/api/employee-skills/${encodeURIComponent(id)}`

/** 员工技能卡与交接看板 API。 */
export const employeeSkillApi = {
  listSkills: ({ status = '', handoverStatus = '', q = '' } = {}) => {
    const query = buildQuery({ status, handover_status: handoverStatus, q })
    return apiGet(query ? `/api/employee-skills?${query}` : '/api/employee-skills')
  },

  getSkill: (id) => apiGet(skillPath(id)),

  createSkill: (payload) => apiPost('/api/employee-skills', payload),

  updateSkill: (id, payload) => apiPut(skillPath(id), payload),

  submitSkill: (id) => apiPost(`${skillPath(id)}/submit`, {}),

  verifySkill: (id) => apiPost(`${skillPath(id)}/verify`, {}),

  archiveSkill: (id) => apiPost(`${skillPath(id)}/archive`, {}),

  deleteSkill: (id) => apiDelete(skillPath(id)),

  listHandoverCandidates: (id, q = '') => {
    const query = buildQuery({ q })
    return apiGet(
      query ? `${skillPath(id)}/handover-candidates?${query}` : `${skillPath(id)}/handover-candidates`
    )
  },

  assignHandover: (id, successorUid) =>
    apiPost(`${skillPath(id)}/handover`, { successor_uid: successorUid }),

  acceptHandover: (id) => apiPost(`${skillPath(id)}/accept`, {})
}
