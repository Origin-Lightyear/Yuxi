import assert from 'node:assert/strict'
import test from 'node:test'

import {
  founderTwinCanManage,
  founderTwinEntryStatusMeta,
  founderTwinHandoffStatusMeta
} from '../../src/utils/founderTwin.js'
import { workItemTarget } from '../../src/utils/workItem.js'

test('founder twin entry statuses use product labels', () => {
  assert.equal(founderTwinEntryStatusMeta('draft').label, '草稿')
  assert.equal(founderTwinEntryStatusMeta('confirmed').label, '已确认')
  assert.equal(founderTwinEntryStatusMeta('invalid').label, '已失效')
})

test('handoff statuses use product labels', () => {
  assert.equal(founderTwinHandoffStatusMeta('open').label, '待老板回答')
  assert.equal(founderTwinHandoffStatusMeta('answered').label, '已回答')
  assert.equal(founderTwinHandoffStatusMeta('closed').label, '已关闭')
})

test('only can_manage twins expose management actions', () => {
  assert.equal(founderTwinCanManage({ can_manage: true }), true)
  assert.equal(founderTwinCanManage({ can_manage: false }), false)
  assert.equal(founderTwinCanManage(null), false)
})

test('handoff work item opens the founder twin page', () => {
  assert.deepEqual(workItemTarget({ source_type: 'handoff', agent_slug: 'boss' }), {
    name: 'FounderTwinComp',
    query: { twin: 'boss' }
  })
})
