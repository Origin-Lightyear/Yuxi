const test = require('node:test')
const assert = require('node:assert/strict')
const { isAllowedExternalUrl } = require('../url_policy.js')

test('放行 http/https/mailto', () => {
  assert.equal(isAllowedExternalUrl('https://example.com/a?b=1'), true)
  assert.equal(isAllowedExternalUrl('http://example.com'), true)
  assert.equal(isAllowedExternalUrl('mailto:a@b.com'), true)
})
test('拒绝危险与本地协议', () => {
  assert.equal(isAllowedExternalUrl('javascript:alert(1)'), false)
  assert.equal(isAllowedExternalUrl('file:///etc/passwd'), false)
  assert.equal(isAllowedExternalUrl('data:text/html,x'), false)
  assert.equal(isAllowedExternalUrl('not a url'), false)
  assert.equal(isAllowedExternalUrl(''), false)
})
test('大小写与首尾空白不影响判定', () => {
  assert.equal(isAllowedExternalUrl('  HTTPS://example.com  '), true)
  assert.equal(isAllowedExternalUrl('JavaScript:alert(1)'), false)
})
