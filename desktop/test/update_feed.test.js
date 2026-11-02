const test = require('node:test')
const assert = require('node:assert/strict')
const { normalizeHttpUrl, resolveUpdateUrl, isAutoUpdateSupported } = require('../update_feed.js')

test('默认从服务地址所在站点根路径追加 /desktop-updates', () => {
  assert.equal(
    resolveUpdateUrl({ backendUrl: 'http://10.69.188.181:5173/' }),
    'http://10.69.188.181:5173/desktop-updates'
  )
  assert.equal(
    resolveUpdateUrl({ backendUrl: 'https://yuxi.example.com/app' }),
    'https://yuxi.example.com/desktop-updates'
  )
})

test('YUXI_UPDATE_URL 覆盖服务地址并去掉末尾斜杠', () => {
  assert.equal(
    resolveUpdateUrl({ envUrl: ' https://cdn.example.com/updates/ ', backendUrl: 'http://10.69.188.181:5173' }),
    'https://cdn.example.com/updates'
  )
})

test('非法覆盖地址被忽略，回落到服务地址', () => {
  for (const envUrl of ['file:///tmp/updates', 'ftp://example.com/updates', 'not a url', '', null]) {
    assert.equal(
      resolveUpdateUrl({ envUrl, backendUrl: 'http://10.69.188.181:5173' }),
      'http://10.69.188.181:5173/desktop-updates'
    )
  }
})

test('服务地址非法时没有更新源', () => {
  assert.equal(resolveUpdateUrl({ backendUrl: 'javascript:alert(1)' }), null)
  assert.equal(resolveUpdateUrl({ backendUrl: undefined }), null)
})

test('规范化丢弃 query/hash 且只接受 http/https', () => {
  assert.equal(normalizeHttpUrl('http://host:5173/a/?x=1#y'), 'http://host:5173/a')
  assert.equal(normalizeHttpUrl('file:///tmp'), null)
  assert.equal(normalizeHttpUrl(123), null)
})

test('只有 Windows x64 安装版启用自动更新', () => {
  assert.equal(isAutoUpdateSupported({ platform: 'win32', isPackaged: true, arch: 'x64' }), true)
  assert.equal(isAutoUpdateSupported({ platform: 'win32', isPackaged: false, arch: 'x64' }), false)
  assert.equal(isAutoUpdateSupported({ platform: 'win32', isPackaged: true, arch: 'arm64' }), false)
  assert.equal(isAutoUpdateSupported({ platform: 'darwin', isPackaged: true, arch: 'x64' }), false)
  assert.equal(isAutoUpdateSupported({ platform: 'linux', isPackaged: true, arch: 'x64' }), false)
})
