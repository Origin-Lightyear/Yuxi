import assert from 'node:assert/strict'
import test from 'node:test'

import {
  digitalEmployeeCapabilities,
  digitalEmployeeSearchText,
  digitalEmployeeStatusMeta,
  normalizeDigitalEmployee
} from '../../src/utils/digitalEmployee.js'

test('normalizeDigitalEmployee 统一 slug/id/profile 并补齐草稿状态', () => {
  const employee = normalizeDigitalEmployee({
    slug: 'analyst',
    name: '经营分析官',
    digital_employee_profile: { position: '经营分析官', capabilities: ['数据查询'] }
  })
  assert.equal(employee.id, 'analyst')
  assert.equal(employee.slug, 'analyst')
  assert.equal(employee.profile.status, 'draft')
  assert.deepEqual(digitalEmployeeCapabilities(employee), ['数据查询'])
  assert.match(digitalEmployeeSearchText(employee), /经营分析官/)
  assert.equal(digitalEmployeeStatusMeta('active').label, '在岗')
  assert.equal(digitalEmployeeStatusMeta('disabled').label, '已停用')
  assert.equal(digitalEmployeeStatusMeta('unknown').label, '草稿')
})
