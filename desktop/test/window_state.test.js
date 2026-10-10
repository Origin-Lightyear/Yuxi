const test = require('node:test')
const assert = require('node:assert/strict')
const { sanitizeWindowState } = require('../window_state.js')

const oneDisplay = [{ x: 0, y: 0, width: 1920, height: 1080 }]

test('合法状态原样返回并归一 maximized', () => {
  assert.deepEqual(
    sanitizeWindowState({ x: 100, y: 50, width: 1280, height: 820, maximized: true }, oneDisplay),
    { x: 100, y: 50, width: 1280, height: 820, maximized: true }
  )
})
test('落在显示器外返回 null', () => {
  assert.equal(sanitizeWindowState({ x: 99999, y: 99999, width: 1280, height: 820 }, oneDisplay), null)
})
test('字段缺失或低于最小尺寸返回 null', () => {
  assert.equal(sanitizeWindowState({ x: 10, y: 10, width: 800, height: 600 }, oneDisplay), null)
  assert.equal(sanitizeWindowState({ x: 10, y: 10, width: 1280 }, oneDisplay), null)
  assert.equal(sanitizeWindowState(null, oneDisplay), null)
})
test('多显示器任一相交即合法', () => {
  const two = [{ x: 0, y: 0, width: 1920, height: 1080 }, { x: 1920, y: 0, width: 1920, height: 1080 }]
  assert.notEqual(sanitizeWindowState({ x: 2000, y: 100, width: 1280, height: 820 }, two), null)
})
