export const SUPPORTED_REPORT_SCHEMA_VERSION = 1

/**
 * 解析报表 JSON 并校验 schema_version 与必填字段；失败返回中文错误，成功透传完整对象（保留未知键）。
 */
export function parseReportArtifact(text) {
  let report
  try {
    report = JSON.parse(text)
  } catch {
    return { ok: false, report: null, error: '报表 JSON 解析失败' }
  }
  if (report === null || typeof report !== 'object' || Array.isArray(report)) {
    return { ok: false, report: null, error: '报表必须是 JSON 对象' }
  }
  const version = report.schema_version
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) {
    return { ok: false, report: null, error: 'schema_version 必须是正整数' }
  }
  if (version > SUPPORTED_REPORT_SCHEMA_VERSION) {
    return {
      ok: false,
      report: null,
      error: `schema_version ${version} 超出当前支持版本 ${SUPPORTED_REPORT_SCHEMA_VERSION}`
    }
  }
  if (typeof report.title !== 'string' || !report.title.trim()) {
    return { ok: false, report: null, error: '缺少必填字段 title' }
  }
  return { ok: true, report, error: null }
}

/**
 * 由线程 ID 与 artifact 相对路径构建报表独立页路由，各段单独编码。
 */
export function buildReportRoute(threadId, artifactPath) {
  const encodedPath = String(artifactPath)
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/')
  return `/report/${encodeURIComponent(threadId)}/${encodedPath}`
}

/**
 * 转义单个 CSV 单元格：含逗号/引号/换行时加引号并把内嵌引号翻倍。
 *
 * 公式注入防护只作用于字符串类型的原始单元格：字符串去前导空白（制表符/CR 等空白本身也被剥离）
 * 后以 `=`、`+`、`-`、`@` 开头时前缀单引号 `'`，使 Excel/WPS 按文本处理；数字与布尔是
 * 类型化值，直接序列化为普通字面量（如 `-5` 保持 `-5`），不做前缀也不加引号。
 */
function escapeCsvCell(value) {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string' && /^[=+\-@]/.test(value.trimStart())) {
    value = `'${value}`
  }
  const text = String(value)
  return /[,"\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

/**
 * 把明细表转成 RFC4180 CSV：首行表头、CRLF 行尾、UTF-8 BOM 前缀，单元格按需加引号。
 */
export function buildTablesCsv(table) {
  const lines = [table.columns, ...table.rows].map((row) =>
    row.map(escapeCsvCell).join(',')
  )
  return `\uFEFF${lines.join('\r\n')}`
}

/**
 * 把报表图表转成 echarts 普通 option；type 仅支持 bar/line/pie，其余抛 Error。
 */
export function buildEchartsOption(chart) {
  const { type, xKey, series = [], data = [] } = chart
  if (type === 'pie') {
    const dataKey = series[0]?.dataKey
    return {
      tooltip: { trigger: 'item' },
      series: [
        {
          type: 'pie',
          data: data.map((row) => ({ name: row[xKey], value: row[dataKey] }))
        }
      ]
    }
  }
  if (type === 'bar' || type === 'line') {
    return {
      tooltip: { trigger: 'axis' },
      legend: { data: series.map((entry) => entry.name) },
      xAxis: { type: 'category', data: data.map((row) => row[xKey]) },
      yAxis: { type: 'value' },
      series: series.map((entry) => ({
        name: entry.name,
        type,
        data: data.map((row) => row[entry.dataKey])
      }))
    }
  }
  throw new Error(`不支持的图表类型：${type}`)
}

/**
 * 生成 CSV 下载文件名：去除路径分隔符与控制字符的标题（空则 report）加本地时间戳。
 */
export function buildCsvDownloadName(title) {
  const base =
    Array.from(String(title ?? ''), (char) => {
      const code = char.charCodeAt(0)
      return char === '/' || char === '\\' || code <= 0x1f || code === 0x7f ? '' : char
    })
      .join('')
      .trim() || 'report'
  const now = new Date()
  const pad = (value) => String(value).padStart(2, '0')
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  return `${base}-${stamp}.csv`
}
