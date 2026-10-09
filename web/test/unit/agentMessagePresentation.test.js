import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveToolPresentation } from '../../src/utils/agentMessagePresentation.js'

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

test('an unresolved ordinary tool collapses when the whole run ends', () => {
  const toolCalls = [{ id: 'unresolved', status: 'running' }]
  const activeState = resolveToolPresentation(toolCalls, new Set(), true)
  const finishedState = resolveToolPresentation(toolCalls, new Set(), false)

  assert.deepEqual(
    activeState.filter((item) => item.expanded).map((item) => item.id),
    ['unresolved']
  )
  assert.deepEqual(
    finishedState.filter((item) => item.expanded).map((item) => item.id),
    []
  )
})

test('active subagent ids cannot expand tools after the whole run ends', () => {
  const state = resolveToolPresentation(
    [{ id: 'subagent-current', status: 'running' }],
    new Set(['subagent-current']),
    false
  )

  assert.deepEqual(
    state.filter((item) => item.expanded).map((item) => item.id),
    []
  )
})
