import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { pathToFileURL } from 'node:url'

import { message } from 'ant-design-vue'
import { createPinia } from 'pinia'
import { createSSRApp } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createServer } from 'vite'

const require = createRequire(import.meta.url)
const vueRequire = createRequire(require.resolve('vue'))
const { renderToString } = await import(pathToFileURL(vueRequire.resolve('@vue/server-renderer')))

for (const outcome of ['resolve', 'reject']) {
  test(`logout navigates while server request is pending and handles ${outcome}`, async (t) => {
    const storage = new Map([['user_token', 'token']])
    const previousStorage = globalThis.localStorage
    globalThis.localStorage = {
      getItem: (key) => storage.get(key) ?? null,
      removeItem: (key) => storage.delete(key)
    }
    const server = await createServer({
      server: { middlewareMode: true },
      appType: 'custom',
      plugins: [{
        name: 'logout-theme-stub',
        enforce: 'pre',
        resolveId: (source) => source.endsWith('/stores/theme') ? '\0logout-theme' : null,
        load: (id) => id === '\0logout-theme' ? 'export const useThemeStore = () => ({})' : null
      }]
    })
    const request = Promise.withResolvers()
    const fetchMock = t.mock.method(globalThis, 'fetch', () => request.promise)
    const warning = t.mock.method(console, 'warn', () => {})
    t.mock.method(message, 'success', () => {})

    try {
      const { default: UserInfoComponent } = await server.ssrLoadModule(
        '/src/components/UserInfoComponent.vue'
      )
      const { useUserStore } = await server.ssrLoadModule('/src/stores/user.js')
      const pinia = createPinia()
      const store = useUserStore(pinia)
      const router = createRouter({
        history: createMemoryHistory(),
        routes: ['/agent', '/login'].map((path) => ({ path, component: { render: () => null } }))
      })
      router.beforeEach((to) => to.path === '/login' && store.isLoggedIn ? '/agent' : true)
      await router.push('/agent')
      const replace = t.mock.method(router, 'replace')
      let state
      const app = createSSRApp({
        setup(props, context) {
          state = UserInfoComponent.setup(props, context)
          return () => null
        }
      })
      app.use(pinia)
      app.use(router)
      await renderToString(app)

      const logout = state.logout()

      assert.equal(store.isLoggedIn, false)
      assert.equal(store.token, '')
      assert.equal(storage.has('user_token'), false)
      assert.deepEqual(fetchMock.mock.calls[0].arguments, [
        '/api/auth/logout',
        { method: 'POST', headers: { Authorization: 'Bearer token' } }
      ])
      assert.equal(replace.mock.callCount(), 1)
      assert.deepEqual(replace.mock.calls[0].arguments, ['/login'])
      await replace.mock.calls[0].result
      assert.equal(router.currentRoute.value.path, '/login')

      const error = new Error('logout unavailable')
      if (outcome === 'reject') request.reject(error)
      else request.resolve(new Response(null, { status: 200 }))
      await logout
      // 等待异步注销完成；node:test 会将未处理的 rejection 判为失败。
      await setImmediate()
      assert.equal(router.currentRoute.value.path, '/login')
      assert.equal(warning.mock.callCount(), outcome === 'reject' ? 1 : 0)
      if (outcome === 'reject') assert.equal(warning.mock.calls[0].arguments[1], error)
    } finally {
      request.resolve(new Response(null, { status: 200 }))
      await server.close()
      globalThis.localStorage = previousStorage
    }
  })
}
