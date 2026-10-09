# Task 3：退出登录竞态修复

## 状态

已完成。

## 变更

- `logout()` 保存旧 token 后立即清理 Pinia 用户状态、本地 token 和 agent store，再使用旧 token 请求后端注销。
- 用户菜单退出登录等待 `logout()` 完成，并使用 `router.replace('/login')`，避免路由守卫看到旧 token 后拦回。
- 增加慢响应场景回归测试，验证请求未完成时本地登录态已经清除。

## 验证

- 容器定向测试：通过。
- 容器前端全量单元测试：21 通过。
- 容器 ESLint：通过。
- 容器前端 build：通过；仅有现有依赖注释和 chunk 体积警告。

## Checklist

- [x] 立即清理 token、Pinia 状态和 agent store
- [x] 使用保存的 token 请求后端注销
- [x] 后端注销失败不恢复登录态
- [x] 使用 `router.replace('/login')`
- [x] 增加并验证竞态回归测试
