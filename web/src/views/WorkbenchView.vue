<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { ChevronRight, Inbox, RefreshCw } from '@lucide/vue'

import PageHeader from '@/components/shared/PageHeader.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import { workbenchApi } from '@/apis/workbench_api'
import {
  WORK_ITEM_STATUS_OPTIONS,
  workItemAction,
  workItemSourceLabel,
  workItemStatusColor,
  workItemStatusLabel,
  workItemTarget
} from '@/utils/workItem'

const router = useRouter()

const loading = ref(false)
const items = ref([])
const total = ref(0)
const limit = 50
const statusFilter = ref('')
const summary = ref({ counts: {}, total: 0, unread: 0 })
const errorText = ref('')
const detailOpen = ref(false)
const detailLoading = ref(false)
const detailItem = ref(null)

const summaryCards = computed(() => [
  { key: 'all', label: '全部待办', value: summary.value.total || 0, tone: 'neutral' },
  { key: 'unread', label: '未读', value: summary.value.unread || 0, tone: 'accent' },
  {
    key: 'waiting_approval',
    label: '待审批',
    value: summary.value.counts?.waiting_approval || 0,
    tone: 'warning'
  },
  { key: 'failed', label: '失败', value: summary.value.counts?.failed || 0, tone: 'danger' }
])

const hasMore = computed(() => items.value.length < total.value)

const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN')
}

const loadSummary = async () => {
  try {
    summary.value = await workbenchApi.getSummary()
  } catch (error) {
    console.warn('加载待办汇总失败:', error)
  }
}

const loadTodos = async ({ append = false } = {}) => {
  loading.value = true
  errorText.value = ''
  const requestOffset = append ? items.value.length : 0
  try {
    const response = await workbenchApi.listTodos({
      status: statusFilter.value,
      limit,
      offset: requestOffset
    })
    const nextItems = response.items || []
    items.value = append ? [...items.value, ...nextItems] : nextItems
    total.value = response.total || 0
  } catch (error) {
    errorText.value = error.message || '加载待办失败'
    message.error(errorText.value)
  } finally {
    loading.value = false
  }
}

const refresh = async () => {
  await Promise.all([loadTodos(), loadSummary()])
}

const loadMore = async () => {
  await loadTodos({ append: true })
}

const handleStatusChange = async () => {
  await loadTodos()
}

const openDetail = async (item) => {
  detailItem.value = item
  detailOpen.value = true
  if (item.unread) {
    try {
      await workbenchApi.markViewed(item.key)
      item.unread = false
      await loadSummary()
    } catch (error) {
      message.error(error.message || '标记已读失败')
    }
  }
  detailLoading.value = true
  try {
    const response = await workbenchApi.getTodo(item.key)
    detailItem.value = response.item || item
  } catch (error) {
    message.error(error.message || '加载待办详情失败')
  } finally {
    detailLoading.value = false
  }
}

const goAction = (item) => {
  detailOpen.value = false
  router.push(workItemTarget(item))
}

onMounted(refresh)
</script>

<template>
  <div class="workbench-page">
    <PageHeader title="待办" :loading="loading">
      <template #actions>
        <a-button type="text" class="refresh-button" @click="refresh">
          <RefreshCw :size="15" />
          刷新
        </a-button>
      </template>
    </PageHeader>

    <PageShoulder>
      <template #filters>
        <a-select
          v-model:value="statusFilter"
          :options="WORK_ITEM_STATUS_OPTIONS"
          class="status-select"
          @change="handleStatusChange"
        />
      </template>
      <template #actions>
        <span class="list-count">共 {{ total }} 条</span>
      </template>
    </PageShoulder>

    <main class="workbench-body">
      <section class="summary-grid" aria-label="待办汇总">
        <div v-for="card in summaryCards" :key="card.key" class="summary-card" :class="`tone-${card.tone}`">
          <span class="summary-label">{{ card.label }}</span>
          <strong class="summary-value">{{ card.value }}</strong>
        </div>
      </section>

      <section class="todo-list" aria-label="待办列表">
        <a-alert
          v-if="errorText && items.length"
          type="error"
          show-icon
          :message="errorText"
          class="list-error"
        >
          <template #action>
            <a-button size="small" type="text" @click="loadMore">重试</a-button>
          </template>
        </a-alert>
        <a-spin :spinning="loading">
          <div v-if="errorText && !items.length" class="empty-state error-state">
            <Inbox :size="34" />
            <p>待办加载失败</p>
            <span>{{ errorText }}</span>
            <a-button size="small" @click="loadTodos()">重试</a-button>
          </div>

          <div v-else-if="!items.length && !loading" class="empty-state">
            <Inbox :size="34" />
            <p>当前没有待办</p>
            <span>数字员工执行、定时任务和审批中断会出现在这里。</span>
          </div>

          <article
            v-for="item in items"
            :key="item.key"
            class="todo-card"
            :class="{ unread: item.unread }"
            tabindex="0"
            @click="openDetail(item)"
            @keydown.enter="openDetail(item)"
          >
            <div class="todo-main">
              <div class="todo-title-row">
                <span v-if="item.unread" class="unread-dot" aria-label="未读"></span>
                <h2>{{ item.title }}</h2>
                <a-tag :color="workItemStatusColor(item.status)">
                  {{ workItemStatusLabel(item.status) }}
                </a-tag>
              </div>
              <div class="todo-meta">
                <span>{{ workItemSourceLabel(item) }}</span>
                <span class="meta-divider">·</span>
                <span>{{ item.agent_name || item.agent_slug || '未知智能体' }}</span>
                <span class="meta-divider">·</span>
                <span>{{ item.initiator_name }}</span>
                <span class="meta-divider">·</span>
                <span>{{ formatTime(item.updated_at) }}</span>
              </div>
              <p v-if="item.approval_summary" class="approval-hint">{{ item.approval_summary }}</p>
            </div>
            <div class="todo-action">
              <span>{{ workItemAction(item).label }}</span>
              <ChevronRight :size="16" />
            </div>
          </article>
        </a-spin>

        <a-button v-if="hasMore" type="text" block class="load-more" :loading="loading" @click="loadMore">
          加载更多
        </a-button>
      </section>
    </main>

    <a-drawer
      v-model:open="detailOpen"
      title="待办详情"
      width="480"
      placement="right"
      :body-style="{ padding: '20px' }"
    >
      <a-spin :spinning="detailLoading">
        <div v-if="detailItem" class="detail-body">
          <div class="detail-title-row">
            <h2>{{ detailItem.title }}</h2>
            <a-tag :color="workItemStatusColor(detailItem.status)">
              {{ workItemStatusLabel(detailItem.status) }}
            </a-tag>
          </div>

          <a-alert
            v-if="detailItem.approval_summary"
            type="warning"
            show-icon
            :message="detailItem.approval_summary"
            description="审批参数和执行上下文在会话中展示，请在会话里完成处理。"
          />

          <dl class="detail-grid">
            <div>
              <dt>来源</dt>
              <dd>{{ workItemSourceLabel(detailItem) }}</dd>
            </div>
            <div>
              <dt>智能体</dt>
              <dd>{{ detailItem.agent_name || detailItem.agent_slug || '—' }}</dd>
            </div>
            <div>
              <dt>发起方</dt>
              <dd>{{ detailItem.initiator_name }}</dd>
            </div>
            <div>
              <dt>更新时间</dt>
              <dd>{{ formatTime(detailItem.updated_at) }}</dd>
            </div>
            <div>
              <dt>运行 ID</dt>
              <dd class="mono">{{ detailItem.run_id || detailItem.source_id }}</dd>
            </div>
            <div>
              <dt>线程 ID</dt>
              <dd class="mono">{{ detailItem.thread_id || '—' }}</dd>
            </div>
          </dl>

          <a-button type="primary" block @click="goAction(detailItem)">
            {{ workItemAction(detailItem).label }}
          </a-button>
        </div>
      </a-spin>
    </a-drawer>
  </div>
</template>

<style scoped lang="less">
.workbench-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--gray-0);
}

.refresh-button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-900);
}

.status-select {
  width: 150px;
}

.list-count {
  color: var(--gray-600);
  font-size: 13px;
}

.workbench-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px var(--page-padding) 40px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}

.summary-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px 18px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);

  &.tone-accent {
    border-color: color-mix(in srgb, var(--main-color) 30%, var(--gray-150));
  }

  &.tone-warning {
    border-color: #f5d58a;
    background: #fffaf0;
  }

  &.tone-danger {
    border-color: #f0c2c2;
    background: #fff7f7;
  }
}

.summary-label {
  color: var(--gray-600);
  font-size: 13px;
}

.summary-value {
  color: var(--gray-2000);
  font-size: 26px;
  line-height: 1;
}

.todo-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.todo-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 16px 18px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
  cursor: pointer;
  transition:
    border-color 0.2s ease,
    box-shadow 0.2s ease,
    transform 0.2s ease;

  &:hover,
  &:focus-visible {
    border-color: var(--gray-300);
    box-shadow: 0 8px 22px color-mix(in srgb, var(--gray-2000) 8%, transparent);
    outline: none;
    transform: translateY(-1px);
  }

  &.unread {
    border-left: 3px solid var(--main-color);
  }
}

.todo-main {
  min-width: 0;
  flex: 1;
}

.todo-title-row,
.detail-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;

  h2 {
    margin: 0;
    color: var(--gray-2000);
    font-size: 15px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.unread-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--main-color);
  flex-shrink: 0;
}

.todo-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
  color: var(--gray-600);
  font-size: 12px;
}

.meta-divider {
  color: var(--gray-300);
}

.approval-hint {
  margin: 8px 0 0;
  color: #9a6700;
  font-size: 12px;
}

.todo-action {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  color: var(--gray-700);
  font-size: 13px;
  white-space: nowrap;
}

.error-state {
  color: var(--error-color, #d4380d);
}

.list-error {
  margin-bottom: 12px;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 72px 16px;
  color: var(--gray-500);
  text-align: center;

  p {
    margin: 0;
    color: var(--gray-800);
    font-weight: 600;
  }

  span {
    font-size: 13px;
  }
}

.load-more {
  margin-top: 8px;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 18px;

  .detail-title-row {
    align-items: flex-start;
    justify-content: space-between;

    h2 {
      white-space: normal;
      font-size: 18px;
    }
  }
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin: 0;

  div {
    min-width: 0;
  }

  dt {
    margin-bottom: 4px;
    color: var(--gray-500);
    font-size: 12px;
  }

  dd {
    margin: 0;
    color: var(--gray-900);
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mono {
    font-family: var(--font-mono, monospace);
    font-size: 12px;
  }
}

@media (max-width: 900px) {
  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .todo-card {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
