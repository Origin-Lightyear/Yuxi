import assert from 'node:assert/strict'
import test from 'node:test'

import {
  resolveReasoningPresentation,
  resolveToolPresentation
} from '../../src/utils/agentMessagePresentation.js'

test('completed reasoning is collapsed by default', () => {
  assert.deepEqual(resolveReasoningPresentation({ status: 'finished' }), {
    active: false,
    expanded: false
  })
})

test('only the active task is expanded while a run is executing', () => {
  const state = resolveToolPresentation([
    { id: 'old', status: 'success' },
    { id: 'current', status: 'running' }
  ])

  assert.deepEqual(
    state.filter((item) => item.expanded).map((item) => item.id),
    ['current']
  )
})

