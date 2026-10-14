import assert from 'node:assert/strict'
import test from 'node:test'

import { isHelpCommand } from '../../src/utils/commandInput.js'

test('isHelpCommand 拦截 trim 后的 /help', () => {
  assert.equal(isHelpCommand('/help'), true)
  assert.equal(isHelpCommand(' /help '), true)
})

test('isHelpCommand 不拦截其他任何输入', () => {
  assert.equal(isHelpCommand('/help extra'), false)
  assert.equal(isHelpCommand('/HELP'), false)
  assert.equal(isHelpCommand('help'), false)
  assert.equal(isHelpCommand(''), false)
  assert.equal(isHelpCommand(null), false)
})
