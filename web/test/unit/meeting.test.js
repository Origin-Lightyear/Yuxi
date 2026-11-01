import assert from 'node:assert/strict'
import test from 'node:test'

import {
  decisionStatusMeta,
  meetingCanJoin,
  meetingCanRun,
  meetingSpeakerName,
  meetingStatusMeta,
  signerStatusMeta,
  speakerTypeLabel
} from '../../src/utils/meeting.js'

test('meeting statuses expose product labels', () => {
  assert.equal(meetingStatusMeta('awaiting_decisions').label, '待拍板')
  assert.equal(meetingStatusMeta('effective').label, '已生效')
  assert.equal(meetingStatusMeta('unknown').label, '未知状态')
  assert.equal(decisionStatusMeta('pending').label, '待签字')
  assert.equal(signerStatusMeta('approved').label, '已同意')
})

test('meeting permissions follow backend projections', () => {
  assert.equal(meetingCanRun({ status: 'draft', can_manage: true }), true)
  assert.equal(meetingCanRun({ status: 'draft', can_manage: false }), false)
  assert.equal(meetingCanRun({ status: 'effective', can_manage: true }), false)
  assert.equal(meetingCanJoin({ can_join: true }), true)
  assert.equal(meetingCanJoin({ can_manage: true }), false)
})

test('meeting speakers fall back through name and slug', () => {
  assert.equal(speakerTypeLabel('digital_employee'), '数字员工')
  assert.equal(speakerTypeLabel('human'), '人')
  assert.equal(meetingSpeakerName({ speaker_name: '市场分身' }), '市场分身')
  assert.equal(meetingSpeakerName({ speaker_slug: 'market' }), 'market')
  assert.equal(meetingSpeakerName({ speaker_uid: 'u1' }), 'u1')
  assert.equal(meetingSpeakerName({}), '未知发言人')
})
