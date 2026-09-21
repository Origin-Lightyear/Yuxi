import assert from 'node:assert/strict'
import test from 'node:test'

import { createPinia, setActivePinia } from 'pinia'
import { createServer } from 'vite'

const storageValues = new Map()
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, String(value)),
  removeItem: (key) => storageValues.delete(key),
  clear: () => storageValues.clear()
}

async function loadUserModule() {
  const server = await createServer({
    server: { middlewareMode: true },
    appType: 'custom'
  })

  return { server, module: await server.ssrLoadModule('/src/stores/user.js') }
}

test('stores saas identity fields from login response', async () => {
  const { server, module } = await loadUserModule()
  try {
    setActivePinia(createPinia())
    globalThis.fetch = async () => new Response(JSON.stringify({
      access_token: 'token',
      user_id: 1,
      username: '张三',
      uid: 'u1',
      role: 'user',
      saas_mode: true,
      department_name: '研发部',
      employee_code: 'E01'
    }), { status: 200 })

    const store = module.useUserStore()
    await store.login({ loginId: '13800000000', password: 'password' })

    assert.equal(store.saasMode, true)
    assert.equal(store.departmentName, '研发部')
    assert.equal(store.employeeCode, 'E01')
  } finally {
    await server.close()
  }
})

test('restores saas identity fields from current user response', async () => {
  const { server, module } = await loadUserModule()
  try {
    setActivePinia(createPinia())
    globalThis.fetch = async () => new Response(JSON.stringify({
      id: 1,
      username: '张三',
      uid: 'u1',
      role: 'user',
      saas_mode: true,
      department_name: '研发部',
      employee_code: 'E01'
    }), { status: 200 })

    const store = module.useUserStore()
    await store.getCurrentUser()

    assert.equal(store.saasMode, true)
    assert.equal(store.departmentName, '研发部')
    assert.equal(store.employeeCode, 'E01')
  } finally {
    await server.close()
  }
})

test('saas profile uses department and employee name instead of local role', async () => {
  const { server, module } = await loadUserModule()
  try {
    assert.equal(
      module.formatUserSummary({
        saasMode: true,
        departmentName: '研发部',
        username: '张三',
        userRole: 'user'
      }),
      '研发部 · 张三'
    )
    assert.equal(
      module.formatUserSummary({
        saasMode: true,
        departmentName: '',
        username: '张三',
        userRole: 'user'
      }),
      '张三'
    )
  } finally {
    await server.close()
  }
})
