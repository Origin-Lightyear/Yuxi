import assert from 'node:assert/strict'
import test from 'node:test'

import {
  workItemAction,
  workItemSourceLabel,
  workItemStatusLabel,
  workItemTarget,
  workItemUnreadLabel
} from '../../src/utils/workItem.js'

test('work item status labels normalize legacy statuses', () => {
  assert.equal(workItemStatusLabel('pending'), '排队中')
  assert.equal(workItemStatusLabel('interrupted'), '已中断')
  assert.equal(workItemStatusLabel('waiting_approval'), '待审批')
  assert.equal(workItemStatusLabel('unknown'), '未知状态')
})

test('work item action opens the existing thread for approval', () => {
  const action = workItemAction({ source_type: 'agent_run', status: 'waiting_approval' })
  assert.equal(action.label, '去对话处理')
  assert.equal(action.key, 'open_thread')
})

test('work item action distinguishes open and closed handoffs', () => {
  const openAction = workItemAction({ source_type: 'handoff', status: 'running' })
  assert.equal(openAction.label, '去处理转交')
  assert.equal(openAction.key, 'open_handoff')
  const closedAction = workItemAction({ source_type: 'handoff', status: 'cancelled' })
  assert.equal(closedAction.label, '查看转交')
  assert.equal(closedAction.key, 'open_handoff')
})

test('work item targets agent threads and founder twin handoffs', () => {
  assert.deepEqual(workItemTarget({ source_type: 'agent_run', thread_id: 'thread-1' }), {
    name: 'AgentCompWithThreadId',
    params: { thread_id: 'thread-1' }
  })
  assert.deepEqual(workItemTarget({ source_type: 'handoff', agent_slug: 'boss' }), {
    name: 'FounderTwinComp',
    query: { twin: 'boss' }
  })
})

test('work item displays source and unread labels', () => {
  assert.equal(workItemSourceLabel({ source_type: 'agent_run', initiator_type: 'digital_employee' }), '数字员工')
  assert.equal(workItemSourceLabel({ source_type: 'agent_run', initiator_type: 'human' }), '人工发起')
  assert.equal(workItemSourceLabel({ source_type: 'handoff' }), '老板分身转交')
  assert.equal(workItemSourceLabel({ source_type: 'decision' }), '会议决议')
  assert.equal(workItemUnreadLabel(true), '未读')
  assert.equal(workItemUnreadLabel(false), '已读')
})

test('work item action asks signers to sign pending meeting decisions', () => {
  const pending = workItemAction({ source_type: 'decision', status: 'waiting_approval' })
  assert.equal(pending.label, '去签字')
  assert.equal(pending.key, 'open_decision')

  const done = workItemAction({ source_type: 'decision', status: 'completed' })
  assert.equal(done.label, '查看决议')
  assert.equal(done.key, 'open_decision')
})

test('work item targets the owning meeting for decisions', () => {
  assert.deepEqual(
    workItemTarget({ source_type: 'decision', source: 'twin_meeting_decision', meeting_id: 22 }),
    { name: 'FounderTwinComp', query: { meeting: '22' } }
  )
  assert.deepEqual(
    workItemTarget({ source_type: 'decision', source: 'ai_meeting_decision', meeting_id: 31 }),
    { name: 'MeetingComp', query: { meeting: '31' } }
  )
})
