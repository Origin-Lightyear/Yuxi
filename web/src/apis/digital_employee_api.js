import { apiDelete, apiGet, apiPost, apiPut, buildQuery } from './base'

const employeePath = (slug) => `/api/digital-employees/${encodeURIComponent(slug)}`

/** 数字员工配置与生命周期 API。 */
export const digitalEmployeeApi = {
  listDigitalEmployees: ({ status = '', q = '' } = {}) => {
    const query = buildQuery({ status, q })
    return apiGet(query ? `/api/digital-employees?${query}` : '/api/digital-employees')
  },

  getDigitalEmployee: (slug) => apiGet(employeePath(slug)),

  createDigitalEmployee: (payload) => apiPost('/api/digital-employees', payload),

  updateDigitalEmployee: (slug, payload) => apiPut(employeePath(slug), payload),

  publishDigitalEmployee: (slug) => apiPost(`${employeePath(slug)}/publish`, {}),

  disableDigitalEmployee: (slug) => apiPost(`${employeePath(slug)}/disable`, {}),

  deleteDigitalEmployee: (slug) => apiDelete(employeePath(slug))
}
