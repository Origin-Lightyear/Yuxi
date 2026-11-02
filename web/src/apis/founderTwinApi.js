import { apiGet, apiPost, apiPut, buildQuery } from './base'

const twinPath = (slug) => `/api/founder-twins/${encodeURIComponent(slug)}`
const entryPath = (slug, entryId) => `${twinPath(slug)}/entries/${entryId}`

/** 老板分身、经验库与转交管理 API。 */
export const founderTwinApi = {
  listTwins: () => apiGet('/api/founder-twins'),

  getTwin: (slug) => apiGet(twinPath(slug)),

  createTwin: (payload) => apiPost('/api/founder-twins', payload),

  updateTwin: (slug, payload) => apiPut(twinPath(slug), payload),

  listEntries: (slug, status = '') => {
    const query = buildQuery({ status })
    return apiGet(query ? `${twinPath(slug)}/entries?${query}` : `${twinPath(slug)}/entries`)
  },

  createEntry: (slug, payload) => apiPost(`${twinPath(slug)}/entries`, payload),

  updateEntry: (slug, entryId, payload) => apiPut(entryPath(slug, entryId), payload),

  confirmEntry: (slug, entryId) => apiPost(`${entryPath(slug, entryId)}/confirm`, {}),

  invalidateEntry: (slug, entryId) => apiPost(`${entryPath(slug, entryId)}/invalidate`, {}),

  listConsultations: (slug) => apiGet(`${twinPath(slug)}/consultations`),

  listHandoffs: (slug) => apiGet(`${twinPath(slug)}/handoffs`),

  updateHandoff: (slug, handoffId, payload) =>
    apiPut(`${twinPath(slug)}/handoffs/${encodeURIComponent(handoffId)}`, payload)
}
