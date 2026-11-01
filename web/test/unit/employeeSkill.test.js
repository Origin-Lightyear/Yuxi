import assert from 'node:assert/strict'
import test from 'node:test'

import {
  captureMethodMeta,
  employeeSkillStatusMeta,
  handoverStatusMeta,
  splitSkillLines
} from '../../src/utils/employeeSkill.js'

test('employee skill statuses expose product labels', () => {
  assert.equal(employeeSkillStatusMeta('draft').label, '草稿')
  assert.equal(employeeSkillStatusMeta('needs_review').label, '待确认')
  assert.equal(employeeSkillStatusMeta('ready').label, '已就绪')
  assert.equal(employeeSkillStatusMeta('unknown').label, '未知状态')
  assert.equal(handoverStatusMeta('assigned').label, '已指派')
  assert.equal(captureMethodMeta('interview').label, '访谈')
})

test('skill text helpers trim blank lines', () => {
  assert.deepEqual(splitSkillLines('确认预算\n\n 核对成本 \n'), ['确认预算', '核对成本'])
  assert.deepEqual(splitSkillLines(''), [])
  assert.deepEqual(splitSkillLines(null), [])
})
