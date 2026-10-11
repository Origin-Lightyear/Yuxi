<template>
  <BaseToolCall
    :tool-call="toolCall"
    :appearance="appearance"
    :default-expanded="defaultExpanded"
    :hide-params="true"
    :status="isError ? 'failed' : ''"
  >
    <template #header v-if="reportTitle">
      <div class="sep-header">
        <span class="note">写入报表</span>
        <span class="separator">|</span>
        <span class="description" :title="reportTitle">{{ reportTitle }}</span>
      </div>
    </template>

    <template #result>
      <div class="report-result">
        <a-alert v-if="isError" type="error" :message="resultText" show-icon />
        <template v-else-if="artifactPath">
          <div class="result-row">
            <span class="label">报表文件</span>
            <span class="value">{{ artifactPath }}</span>
          </div>
          <router-link v-if="reportLink" class="open-report" :to="reportLink">
            打开报表
          </router-link>
        </template>
        <pre v-else-if="resultText" class="preview">{{ resultText }}</pre>
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import BaseToolCall from '../BaseToolCall.vue'
import { parseToolCallArgs } from '../toolRegistry'
import { buildReportRoute } from '@/utils/reportWorkspace'

const props = defineProps({
  toolCall: {
    type: Object,
    required: true
  },
  appearance: {
    type: String,
    default: 'card'
  },
  defaultExpanded: {
    type: Boolean,
    default: false
  }
})

const route = useRoute()

const parsedArgs = computed(() => parseToolCallArgs(props.toolCall))

// report 参数可能是对象或 JSON 字符串；解析失败返回空标题，让 BaseToolCall 兜底展示，不在卡片内吞错。
const reportPayload = computed(() => {
  const report = parsedArgs.value.report
  if (typeof report !== 'string') return report ?? null
  try {
    return JSON.parse(report)
  } catch {
    return null
  }
})

const reportTitle = computed(() => {
  const title = reportPayload.value?.title
  return typeof title === 'string' && title.trim() ? title : ''
})

const resultText = computed(() => {
  const content = props.toolCall?.tool_call_result?.content ?? props.toolCall?.result
  return typeof content === 'string' ? content : ''
})

const isError = computed(() => resultText.value.trimStart().startsWith('Error:'))

const artifactPath = computed(() => {
  const match = resultText.value.match(/报表已写入\s+(.+?)（schema_version=/)
  return match ? match[1].trim() : ''
})

const reportLink = computed(() => {
  const threadId = route.params.thread_id
  if (!artifactPath.value || typeof threadId !== 'string' || !threadId) return ''
  return buildReportRoute(threadId, artifactPath.value)
})
</script>

<style lang="less" scoped>
.report-result {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.result-row {
  display: flex;
  gap: 8px;
  align-items: baseline;
  min-width: 0;
  font-size: 12px;
}

.label {
  flex: 0 0 auto;
  color: var(--gray-500);
}

.value {
  min-width: 0;
  color: var(--gray-800);
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  overflow-wrap: anywhere;
}

.open-report {
  align-self: flex-start;
  font-size: 12px;
  color: var(--main-600);
  font-weight: 500;

  &:hover {
    color: var(--main-700);
  }
}

.preview {
  max-height: 220px;
  margin: 0;
  padding: 8px;
  overflow: auto;
  color: var(--gray-800);
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
}
</style>
