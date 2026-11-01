import { apiGet, apiPost, buildQuery } from './base'

/** 分身会议与 AI 会议 API。 */
export const meetingApi = {
  listMeetings: ({ kind = '', limit = 100 } = {}) => {
    const query = buildQuery({ kind, limit })
    return apiGet(query ? `/api/meetings?${query}` : '/api/meetings')
  },

  getMeeting: (id) => apiGet(`/api/meetings/${encodeURIComponent(id)}`),

  createMeeting: (payload) => apiPost('/api/meetings', payload),

  runMeeting: (id, payload = {}) => apiPost(`/api/meetings/${encodeURIComponent(id)}/run`, payload),

  addMessage: (id, payload) => apiPost(`/api/meetings/${encodeURIComponent(id)}/messages`, payload),

  signDecision: (id, decisionId, payload) =>
    apiPost(
      `/api/meetings/${encodeURIComponent(id)}/decisions/${encodeURIComponent(decisionId)}/sign`,
      payload
    )
}
