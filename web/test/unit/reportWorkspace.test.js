import assert from 'node:assert/strict'
import test from 'node:test'

import { createServer } from 'vite'

async function loadReportWorkspace() {
  const server = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom'
  })
  try {
    return await server.ssrLoadModule('/src/utils/reportWorkspace.js')
  } finally {
    await server.close()
  }
}

const {
  SUPPORTED_REPORT_SCHEMA_VERSION,
  parseReportArtifact,
  buildReportRoute,
  buildTablesCsv,
  buildEchartsOption,
  buildCsvDownloadName
} = await loadReportWorkspace()

const minimalReport = {
  schema_version: 1,
  title: '2026 年 9 月收入报表',
  generated_at: '2026-10-10T08:00:00Z',
  summary: '本月收入环比上升',
  metrics: [],
  tables: [],
  charts: []
}

test('SUPPORTED_REPORT_SCHEMA_VERSION 固定为 1', () => {
  assert.equal(SUPPORTED_REPORT_SCHEMA_VERSION, 1)
})

test('parseReportArtifact 最小合法报表透传 title 并保留未知键', () => {
  const result = parseReportArtifact(
    JSON.stringify({ ...minimalReport, future_extension: { note: '保留' } })
  )

  assert.equal(result.ok, true)
  assert.equal(result.error, null)
  assert.equal(result.report.title, '2026 年 9 月收入报表')
  assert.deepEqual(result.report.future_extension, { note: '保留' })
})

test('parseReportArtifact schema_version 超出支持版本时失败', () => {
  const result = parseReportArtifact(JSON.stringify({ ...minimalReport, schema_version: 2 }))

  assert.equal(result.ok, false)
  assert.equal(result.report, null)
  assert.equal(typeof result.error, 'string')
  assert.equal(result.error.includes('schema_version'), true)
})

test('parseReportArtifact 非法 JSON 文本时失败', () => {
  const result = parseReportArtifact('{"schema_version": 1, "title": ')

  assert.equal(result.ok, false)
  assert.equal(result.report, null)
  assert.equal(typeof result.error, 'string')
})

test('parseReportArtifact 缺 title 时失败', () => {
  const withoutTitle = { ...minimalReport }
  delete withoutTitle.title
  const result = parseReportArtifact(JSON.stringify(withoutTitle))

  assert.equal(result.ok, false)
  assert.equal(result.report, null)
  assert.equal(result.error.includes('title'), true)
})

test('parseReportArtifact schema_version 非正整数或缺失时失败', () => {
  for (const schemaVersion of [0, -1, '1', null, undefined]) {
    const result = parseReportArtifact(
      JSON.stringify({ ...minimalReport, schema_version: schemaVersion })
    )

    assert.equal(result.ok, false, `schema_version=${schemaVersion} 必须失败`)
    assert.equal(result.report, null)
    assert.equal(result.error.includes('schema_version'), true)
  }

  const withoutVersion = { ...minimalReport }
  delete withoutVersion.schema_version
  const missing = parseReportArtifact(JSON.stringify(withoutVersion))

  assert.equal(missing.ok, false)
  assert.equal(missing.report, null)
  assert.equal(missing.error.includes('schema_version'), true)
})

test('parseReportArtifact 非对象 JSON 载荷返回失败而不抛错', () => {
  for (const payload of ['null', '[]', '123', '"文本"']) {
    const result = parseReportArtifact(payload)

    assert.equal(result.ok, false, `${payload} 必须失败`)
    assert.equal(result.report, null)
    assert.equal(typeof result.error, 'string')
  }
})

test('buildReportRoute 对线程与路径逐段编码', () => {
  assert.equal(
    buildReportRoute('t 1', 'u1/project/outputs/reports/a b.json'),
    '/report/t%201/u1/project/outputs/reports/a%20b.json'
  )
})

test('buildTablesCsv 按 RFC4180 转义并带 BOM 与 CRLF 行尾', () => {
  const csv = buildTablesCsv({ columns: ['a,b', '说"明'], rows: [['1', 'x\ny']] })

  assert.equal(csv.startsWith('\uFEFF'), true)
  assert.equal(csv, '\uFEFF"a,b","说""明"\r\n1,"x\ny"')
})

test('buildTablesCsv 数字/布尔转字符串且 null/undefined 转空串', () => {
  const csv = buildTablesCsv({ columns: ['n', 'b', 'z', 'u'], rows: [[1, true, null, undefined]] })

  assert.equal(csv, '\uFEFFn,b,z,u\r\n1,true,,')
})

test('buildEchartsOption bar 图按 xKey 与 dataKey 生成普通 option', () => {
  const option = buildEchartsOption({
    type: 'bar',
    xKey: 'month',
    series: [{ name: '收入', dataKey: 'revenue' }],
    data: [
      { month: '1 月', revenue: 120 },
      { month: '2 月', revenue: 200 }
    ]
  })

  assert.equal(option.series[0].type, 'bar')
  assert.equal(option.series[0].name, '收入')
  assert.deepEqual(option.series[0].data, [120, 200])
  assert.deepEqual(option.xAxis.data, ['1 月', '2 月'])
})

test('buildTablesCsv 无特殊字符的普通单元格不加引号', () => {
  const csv = buildTablesCsv({ columns: ['说明'], rows: [['普通文本']] })

  assert.equal(csv, '\uFEFF说明\r\n普通文本')
})

test('buildTablesCsv 以公式字符开头的字符串单元格前缀单引号防注入', () => {
  const csv = buildTablesCsv({
    columns: ['f', 'c'],
    rows: [['=HYPERLINK("http://evil.example","点我")', '+cmd'], ['  =2+2', '@SUM(1)']]
  })

  assert.equal(
    csv,
    '\uFEFFf,c\r\n"\'=HYPERLINK(""http://evil.example"",""点我"")",\'+cmd\r\n\'  =2+2,\'@SUM(1)'
  )
})

test('buildTablesCsv 数字与布尔不做公式防护且序列化为普通字面量', () => {
  const csv = buildTablesCsv({ columns: ['n', 'd', 'b'], rows: [[-5, 12.5, true]] })

  assert.equal(csv, '\uFEFFn,d,b\r\n-5,12.5,true')
})

test('buildEchartsOption line 图与 bar 同理生成 option', () => {
  const option = buildEchartsOption({
    type: 'line',
    xKey: 'month',
    series: [{ name: '趋势', dataKey: 'count' }],
    data: [{ month: '1 月', count: 5 }]
  })

  assert.equal(option.series[0].type, 'line')
  assert.deepEqual(option.series[0].data, [5])
})

test('buildEchartsOption pie 图取 series[0].dataKey 作为 value', () => {
  const option = buildEchartsOption({
    type: 'pie',
    xKey: 'name',
    series: [{ name: '占比', dataKey: 'amount' }],
    data: [
      { name: '华东', amount: 30 },
      { name: '华南', amount: 70 }
    ]
  })

  assert.equal(option.series[0].type, 'pie')
  assert.equal(option.series[0].data[0].name, '华东')
  assert.equal(option.series[0].data[0].value, 30)
  assert.equal(option.series[0].data[1].value, 70)
})

test('buildEchartsOption 非法图表类型抛 Error', () => {
  assert.throws(
    () =>
      buildEchartsOption({
        type: 'scatter',
        xKey: 'x',
        series: [{ name: '点', dataKey: 'v' }],
        data: []
      }),
    /scatter/
  )
})

test('buildCsvDownloadName 净化标题并拼时间戳', () => {
  assert.match(buildCsvDownloadName('月度/收入:报表'), /^月度收入:报表-\d{8}-\d{6}\.csv$/)
  assert.match(buildCsvDownloadName('..\\etc/passwd'), /^\.{2}etcpasswd-\d{8}-\d{6}\.csv$/)
  assert.match(buildCsvDownloadName('带\u0000控制符'), /^带控制符-\d{8}-\d{6}\.csv$/)
})

test('buildCsvDownloadName 标题为空时回退 report', () => {
  assert.match(buildCsvDownloadName(''), /^report-\d{8}-\d{6}\.csv$/)
  assert.match(buildCsvDownloadName('   '), /^report-\d{8}-\d{6}\.csv$/)
})
