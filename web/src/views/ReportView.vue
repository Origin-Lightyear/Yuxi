<template>
  <div class="report-view layout-container">
    <div v-if="loading" class="report-state">
      <a-spin size="large" tip="正在加载报表..." />
    </div>

    <a-alert
      v-else-if="errorMessage"
      class="report-error"
      type="error"
      :message="errorMessage"
      show-icon
    />

    <template v-else-if="report">
      <header class="report-header">
        <h2 class="report-title">{{ report.title }}</h2>
        <div class="report-meta">
          <span v-if="report.generated_at" class="report-generated-at">
            生成时间：{{ report.generated_at }}
          </span>
          <a-badge :status="validationBadge.status" :text="validationBadge.text" />
        </div>
        <p v-if="report.summary" class="report-summary">{{ report.summary }}</p>
      </header>

      <section v-if="report.metrics?.length" class="report-section">
        <h3>指标</h3>
        <a-table
          :columns="metricColumns"
          :data-source="report.metrics"
          row-key="key"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'source'">
              <a-button
                v-if="record.source !== null && record.source !== undefined"
                type="link"
                size="small"
                @click="openSourceDrawer(record.source)"
              >
                查看
              </a-button>
            </template>
          </template>
        </a-table>
      </section>

      <section v-if="report.tables?.length" class="report-section">
        <h3>明细表</h3>
        <div v-for="(table, tableIndex) in report.tables" :key="tableIndex" class="report-table">
          <div class="report-table-header">
            <span class="report-table-title">{{ table.title }}</span>
            <a-button size="small" @click="exportTableCsv(table)">导出 CSV</a-button>
          </div>
          <a-table
            :columns="buildTableColumns(table)"
            :data-source="table.rows"
            :row-key="rowIndexKey"
            size="small"
            :pagination="false"
          />
        </div>
      </section>

      <section v-if="report.charts?.length" class="report-section">
        <h3>图表</h3>
        <div class="report-charts">
          <div v-for="(chart, chartIndex) in report.charts" :key="chartIndex" class="report-chart">
            <div :ref="(el) => setChartRef(el, chartIndex)" class="report-chart-canvas"></div>
          </div>
        </div>
      </section>

      <a-drawer
        v-model:open="sourceDrawerOpen"
        title="指标来源"
        placement="right"
        :width="420"
      >
        <pre class="report-source-pre">{{ activeSourceText }}</pre>
      </a-drawer>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUpdated, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { threadApi } from '@/apis/agent_api'
import { init } from '@/utils/dashboardCharts'
import {
  parseReportArtifact,
  buildTablesCsv,
  buildEchartsOption,
  buildCsvDownloadName
} from '@/utils/reportWorkspace'

const route = useRoute()

const loading = ref(true)
const errorMessage = ref('')
const report = ref(null)

const metricColumns = [
  { title: '指标', dataIndex: 'label', key: 'label' },
  { title: '值', dataIndex: 'value', key: 'value' },
  { title: '单位', dataIndex: 'unit', key: 'unit' },
  { title: '来源', key: 'source' }
]

// 校验状态徽标：agent_declared 需向读者明示未独立校验。
const validationBadge = computed(() => {
  const status = report.value?.validation_status ?? 'agent_declared'
  return status === 'agent_declared'
    ? { status: 'warning', text: 'Agent 自评（未独立校验）' }
    : { status: 'success', text: status }
})

// a-table 行 key：明细行是纯数组，用下标保证稳定。
const rowIndexKey = (record, index) => index

// catch-all 参数在 Vue Router 4 中可能是字符串或段数组，统一拼回 / 分隔路径。
function normalizeCatchAllPath(rawPath) {
  if (Array.isArray(rawPath)) return rawPath.join('/')
  return typeof rawPath === 'string' ? rawPath : ''
}

function buildTableColumns(table) {
  return (table.columns ?? []).map((title, index) => ({
    title,
    dataIndex: index,
    key: String(index)
  }))
}

async function loadReport() {
  loading.value = true
  errorMessage.value = ''
  report.value = null

  const threadId = route.params.thread_id
  const artifactPath = normalizeCatchAllPath(route.params.path)
  if (!threadId || !artifactPath) {
    errorMessage.value = '报表链接不完整，无法定位报表文件'
    loading.value = false
    return
  }

  try {
    // 鉴权与线程归属由后端在 artifact API 处执行；404 无权访问与网络错误同走此错误分支。
    const res = await threadApi.previewThreadArtifact(threadId, artifactPath)
    const text = await res.text()
    const parsed = parseReportArtifact(text)
    if (!parsed.ok) {
      errorMessage.value = parsed.error
      return
    }
    report.value = parsed.report
  } catch (error) {
    errorMessage.value = `报表加载失败：${error?.message || '网络错误'}`
  } finally {
    loading.value = false
  }
}

// CSV 导出：Blob + 临时 a 标签触发浏览器下载，随后释放 object URL。
function exportTableCsv(table) {
  if (!Array.isArray(table.rows) || table.rows.length === 0) return
  const csv = buildTablesCsv(table)
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = buildCsvDownloadName(table.title)
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

const sourceDrawerOpen = ref(false)
const activeSourceText = ref('')

function openSourceDrawer(source) {
  activeSourceText.value = typeof source === 'string' ? source : JSON.stringify(source, null, 2)
  sourceDrawerOpen.value = true
}

// 图表实例按索引持有；DOM 更新后重绘，卸载时统一 dispose 防止实例泄漏。
const chartEls = ref([])
const chartInstances = new Map()

function setChartRef(el, index) {
  if (el) chartEls.value[index] = el
}

function renderCharts() {
  const charts = report.value?.charts ?? []
  charts.forEach((chart, index) => {
    const el = chartEls.value[index]
    if (!el) return
    try {
      const option = buildEchartsOption(chart)
      let instance = chartInstances.get(index)
      if (!instance) {
        instance = init(el)
        chartInstances.set(index, instance)
      }
      instance.setOption(option)
    } catch (error) {
      console.error('报表图表渲染失败:', error)
    }
  })
}

onMounted(() => {
  loadReport()
})

onUpdated(() => {
  renderCharts()
})

onUnmounted(() => {
  chartInstances.forEach((instance) => instance.dispose())
  chartInstances.clear()
})
</script>

<style scoped lang="less">
.report-view {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px 32px;
}

.report-state {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}

.report-error {
  margin-top: 16px;
}

.report-header {
  margin-bottom: 8px;
}

.report-title {
  margin: 0 0 8px 0;
}

.report-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 8px;
}

.report-generated-at {
  font-size: 12px;
  color: var(--gray-500);
}

.report-summary {
  margin: 0 0 8px 0;
  color: var(--gray-600);
}

.report-section {
  margin-top: 24px;

  h3 {
    margin: 0 0 12px 0;
  }
}

.report-table {
  margin-bottom: 20px;
}

.report-table-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.report-table-title {
  font-weight: 500;
  color: var(--gray-800);
}

.report-charts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: 16px;
}

.report-chart-canvas {
  height: 320px;
  width: 100%;
}

.report-source-pre {
  margin: 0;
  padding: 12px;
  overflow: auto;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
