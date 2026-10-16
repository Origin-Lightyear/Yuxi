import { apiDelete, apiGet } from './base'

/** Agent 记忆条目 API。 */
export const agentMemoryApi = {
  list: (slug) => apiGet(`/api/agent/${slug}/memories`),
  remove: (slug, memoryId) => apiDelete(`/api/agent/${slug}/memories/${memoryId}`)
}
