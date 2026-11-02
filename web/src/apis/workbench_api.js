import { apiGet, apiPost, buildQuery } from './base'

const todoPath = (key) => `/api/workbench/todos/${encodeURIComponent(key)}`

/** 待办收件箱 API。 */
export const workbenchApi = {
  listTodos: ({ status = '', limit = 50, offset = 0 } = {}) => {
    const query = buildQuery({ status, limit, offset })
    return apiGet(query ? `/api/workbench/todos?${query}` : '/api/workbench/todos')
  },

  getTodo: (key) => apiGet(todoPath(key)),

  markViewed: (key) => apiPost(`${todoPath(key)}/viewed`, {}),

  getSummary: () => apiGet('/api/workbench/todos/summary')
}
