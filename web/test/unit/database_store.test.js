import test from 'node:test'
import assert from 'node:assert/strict'

import { createPinia, setActivePinia } from 'pinia'
import { createApp } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'
import { message } from 'ant-design-vue'

const storageValues = new Map()
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key),
  clear: () => storageValues.clear()
}

test('database SaaS 模式强制使用 accessible 列表并保留权限字段', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const { databaseApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
    const user = useUserStore()
    user.saasMode = true
    user.userRole = 'admin'
    databaseApi.getDatabases = async () => { throw new Error('SaaS 不应调用管理员列表') }
    databaseApi.getAccessibleDatabases = async () => ({ databases: [{ kb_id: 'tenant-kb', can_manage: false }] })
    const store = useDatabaseStore()
    await store.loadDatabases()
    assert.deepEqual(store.databases, [{ kb_id: 'tenant-kb', can_manage: false }])
  } finally {
    await server.close()
  }
})

test('database reloads with the administrator endpoint after identity restoration', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const { databaseApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
    const user = useUserStore()
    const calls = []
    databaseApi.getAccessibleDatabases = async () => {
      calls.push('accessible')
      return { databases: [] }
    }
    databaseApi.getDatabases = async () => {
      calls.push('admin')
      return { databases: [{ kb_id: 'admin-kb' }] }
    }

    const store = useDatabaseStore()
    await store.loadDatabases()
    user.userRole = 'admin'
    await new Promise((resolve) => setTimeout(resolve, 0))

    assert.deepEqual(calls, ['accessible', 'admin'])
    assert.deepEqual(store.databases, [{ kb_id: 'admin-kb' }])
  } finally {
    await server.close()
  }
})

test('agent resources reload after identity restoration', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const { agentApi, databaseApi, mcpApi, skillApi } = await server.ssrLoadModule('/src/apis/index.js')
    const { useAgentStore } = await server.ssrLoadModule('/src/stores/agent.js')
    const user = useUserStore()
    databaseApi.getAccessibleDatabases = async () =>
      user.uid ? { databases: [{ kb_id: 'tenant-kb' }] } : { databases: [] }
    agentApi.getAgents = async () => ({ agents: [] })
    mcpApi.getMcpServers = async () => ({ data: [] })
    skillApi.listAccessibleSkills = async () => ({ data: [] })

    const store = useAgentStore()
    await store.initialize()
    assert.deepEqual(store.availableKnowledgeBases, [])

    user.uid = 'employee-001'
    await new Promise((resolve) => setTimeout(resolve, 0))

    assert.deepEqual(store.availableKnowledgeBases, [{ kb_id: 'tenant-kb' }])
  } finally {
    await server.close()
  }
})

test('database 员工读取目录、文档及上传请求由后端授权', async () => {
  const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  const originalFetch = globalThis.fetch
  try {
    setActivePinia(createPinia())
    const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
    const { databaseApi, documentApi, fileApi, queryApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const user = useUserStore()
    user.userRole = 'user'
    user.saasMode = true
    user.token = 'employee-token'
    const paths = []
    globalThis.fetch = async (url, options) => {
      paths.push(url)
      if (options.body instanceof FormData) {
        assert.equal(options.headers['Content-Type'], undefined, '浏览器必须自动补全 multipart boundary')
      }
      return new Response(JSON.stringify({ status: 'success', items: [] }), { status: 200 })
    }
    await databaseApi.getDatabaseInfo('tenant-kb')
    await documentApi.listDocuments('tenant-kb')
    await documentApi.getDocumentBasicInfo('tenant-kb', 'doc')
    await documentApi.getDocumentContent('tenant-kb', 'doc')
    await documentApi.addUploadedDocuments('tenant-kb', ['minio://doc'])
    await documentApi.addDocuments('tenant-kb', ['minio://doc'])
    await documentApi.deleteDocument('tenant-kb', 'doc')
    await documentApi.moveDocument('tenant-kb', 'doc', 'folder')
    await fileApi.uploadFile(new File(['content'], 'file.txt'), 'tenant-kb')
    await queryApi.getKnowledgeBaseQueryParams('tenant-kb')
    assert.equal(paths.length, 10)
    assert.ok(paths.every((path) => path.includes('tenant-kb')))
  } finally {
    globalThis.fetch = originalFetch
    await server.close()
  }
})

test('从二级目录点击全部文件会清空 parent_id 并返回根目录', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })

  try {
    const warnings = []
    t.mock.method(console, 'warn', (...args) => warnings.push(args))
    const pinia = createPinia()
    const app = createApp({})
    app.use(pinia)
    app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
    setActivePinia(pinia)
    const { documentApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
    const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
    const requests = []

    documentApi.listDocuments = async (kbId, params) => {
      requests.push({ kbId, params })
      return {
        items: [],
        page: 1,
        page_size: 100,
        total: 0,
        has_more: false,
        path_prefix: ''
      }
    }

    const store = app.runWithContext(() => useDatabaseStore())
    store.kbId = 'kb_1'
    store.fileBrowser.parentId = 'folder_2'
    store.folderBreadcrumbs = [
      { file_id: null, filename: '全部文件', path_prefix: '' },
      { file_id: 'folder_2', filename: '二级目录', path_prefix: '' }
    ]

    await store.goToFolder(0)

    assert.equal(store.fileBrowser.parentId, null)
    assert.deepEqual(store.folderBreadcrumbs, [
      { file_id: null, filename: '全部文件', path_prefix: '' }
    ])
    assert.deepEqual(requests, [
      {
        kbId: 'kb_1',
        params: {
          page: 1,
          page_size: 100,
          status: 'all',
          recursive: false
        }
      }
    ])
    assert.deepEqual(warnings, [])
  } finally {
    await server.close()
  }
})

test('知识库提交跨账号返回时，不把旧入队任务登记给新账号', async (t) => {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  const pinia = createPinia()
  const app = createApp({})
  app.use(pinia)
  app.use(createRouter({ history: createMemoryHistory(), routes: [] }))
  setActivePinia(pinia)
  const { documentApi, databaseApi } = await server.ssrLoadModule('/src/apis/knowledge_api.js')
  const { useDatabaseStore } = await server.ssrLoadModule('/src/stores/database.js')
  const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
  const { useTaskerStore } = await server.ssrLoadModule('/src/stores/tasker.js')
  const database = app.runWithContext(() => useDatabaseStore())
  const user = useUserStore()
  const tasker = useTaskerStore()
  t.mock.method(message, 'success', () => {})
  t.mock.method(databaseApi, 'getDatabaseInfo', async () => ({ stats: { processing_count: 0 } }))
  // 身份变化 watcher 会在登出/恢复间隙触发列表重载，单元测试不打真实接口
  t.mock.method(databaseApi, 'getDatabases', async () => ({ databases: [] }))
  t.mock.method(databaseApi, 'getAccessibleDatabases', async () => ({ databases: [] }))
  t.mock.method(documentApi, 'listDocuments', async () => ({
    items: [],
    stats: { processing_count: 0 }
  }))
  try {
    for (const [action, apiMethod, args] of [
      ['addFiles', 'addDocuments', [{ items: ['fixture'], contentType: 'file', params: {} }]],
      ['parseFiles', 'parseDocuments', [['file']]],
      ['parsePendingFiles', 'parsePendingDocuments', []],
      ['indexFiles', 'indexDocuments', [['file']]],
      ['indexPendingFiles', 'indexPendingDocuments', []]
    ]) {
      await t.test(action, async () => {
        database.kbId = 'test-kb'
        user.token = `old-${action}`
        user.userRole = 'admin'
        let resolve
        t.mock.method(
          documentApi,
          apiMethod,
          () =>
            new Promise((done) => {
              resolve = done
            })
        )
        const pending = database[action](...args)
        user.logout()
        user.token = `new-${action}`
        user.userRole = 'admin'
        resolve({ status: 'queued', task_id: 'old-task' })
        assert.equal(await pending, true)
        assert.deepEqual(tasker.tasks, [])
        const current = database[action](...args)
        resolve({ status: 'queued', task_id: `new-${action}` })
        assert.equal(await current, true)
        assert.equal(tasker.tasks[0].id, `new-${action}`)
        tasker.reset()
      })
    }
  } finally {
    database.stopAutoRefresh()
    tasker.$dispose()
    await server.close()
  }
})
