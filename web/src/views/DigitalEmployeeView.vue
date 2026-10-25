<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { Plus, RefreshCw, Users } from '@lucide/vue'

import PageHeader from '@/components/shared/PageHeader.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import ExtensionCardGrid from '@/components/extensions/ExtensionCardGrid.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import AgentEditModal from '@/components/model-management/AgentEditModal.vue'
import { agentApi } from '@/apis/agent_api'
import { digitalEmployeeApi } from '@/apis/digital_employee_api'
import { useUserStore } from '@/stores/user'
import { normalizeAgentBackendOption } from '@/utils/agentConfigUtils'
import { getShareConfigLabel } from '@/utils/shareConfig'
import {
  digitalEmployeeCapabilities,
  digitalEmployeeSearchText,
  digitalEmployeeStatusMeta,
  normalizeDigitalEmployee
} from '@/utils/digitalEmployee'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const employees = ref([])
const summary = ref({ active: 0, draft: 0, disabled: 0 })
const searchQuery = ref('')
const statusFilter = ref('all')
const selected = ref(null)
const detailOpen = ref(false)
const editModalRef = ref(null)
const backendOptions = ref([])

const canCreate = computed(() => userStore.isAdmin)
const showStatusFilter = computed(() => userStore.isAdmin)
const statusOptions = [
  { value: 'all', label: '全部状态' },
  { value: 'active', label: '在岗' },
  { value: 'draft', label: '草稿' },
  { value: 'disabled', label: '已停用' }
]

const filteredEmployees = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase()
  return employees.value.filter((employee) => {
    if (statusFilter.value !== 'all' && employee.profile.status !== statusFilter.value) return false
    return !keyword || digitalEmployeeSearchText(employee).includes(keyword)
  })
})

const statusText = computed(() => {
  const active = summary.value.active || 0
  const draft = summary.value.draft || 0
  return canCreate.value ? `${active} 个在岗 · ${draft} 个草稿` : `${active} 个在岗数字员工`
})

const selectedStatus = computed(() => digitalEmployeeStatusMeta(selected.value?.profile?.status))
const selectedCapabilities = computed(() => digitalEmployeeCapabilities(selected.value))
const selectedShareLabel = computed(() =>
  selected.value?.share_config ? getShareConfigLabel(selected.value.share_config) : '—'
)
const resourceSummary = computed(() => {
  const context = selected.value?.config_json?.context || {}
  const describe = (value) => {
    if (value === 'all') return '全部'
    if (Array.isArray(value)) return `${value.length} 项`
    return value ? '已配置' : '未配置'
  }
  return [
    { label: '模型', value: context.model || '默认模型' },
    { label: '知识库', value: describe(context.knowledges) },
    { label: 'Skills', value: describe(context.skills) },
    { label: '工具', value: describe(context.tools) }
  ]
})

const employeeSubtitle = (employee) => {
  return [employee.profile.position, employee.profile.department].filter(Boolean).join(' · ') || '岗位待配置'
}

const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN')
}

const loadEmployees = async () => {
  loading.value = true
  try {
    const response = await digitalEmployeeApi.listDigitalEmployees()
    employees.value = (response.employees || []).map(normalizeDigitalEmployee)
    summary.value = response.summary || { active: 0, draft: 0, disabled: 0 }
    if (selected.value) {
      selected.value = employees.value.find((item) => item.slug === selected.value.slug) || null
      if (!selected.value) detailOpen.value = false
    }
  } catch (error) {
    message.error(error.message || '加载数字员工失败')
  } finally {
    loading.value = false
  }
}

const loadBackends = async () => {
  try {
    const response = await agentApi.getAgentBackends()
    backendOptions.value = (response.backends || [])
      .filter((backend) => backend.backend_id !== 'SubAgentBackend')
      .map(normalizeAgentBackendOption)
  } catch (error) {
    message.error(error.message || '加载智能体后端失败')
  }
}

const openCreate = () => {
  if (!canCreate.value) return
  editModalRef.value?.openCreate()
}

const openEdit = (employee) => {
  detailOpen.value = false
  editModalRef.value?.openEdit(employee)
}

const openDetail = async (employee) => {
  const targetSlug = employee.slug
  selected.value = employee
  detailOpen.value = true
  try {
    const response = await digitalEmployeeApi.getDigitalEmployee(targetSlug)
    if (selected.value?.slug === targetSlug) {
      selected.value = normalizeDigitalEmployee(response.employee)
    }
  } catch (error) {
    message.error(error.message || '加载数字员工详情失败')
  }
}

const goChat = (employee) => {
  detailOpen.value = false
  router.push({ name: 'AgentComp', query: { agent_id: employee.slug } })
}

const handleSaved = async () => {
  await loadEmployees()
}

const publish = (employee) => {
  Modal.confirm({
    title: `发布「${employee.name}」？`,
    content: '发布后，共享范围内的用户可以在数字员工页和对话中看到并使用它。',
    okText: '发布',
    async onOk() {
      await digitalEmployeeApi.publishDigitalEmployee(employee.slug)
      message.success('数字员工已发布')
      await loadEmployees()
    }
  })
}

const disable = (employee) => {
  Modal.confirm({
    title: `停用「${employee.name}」？`,
    content: '停用后不能发起新对话、新定时任务或新 IM 绑定；历史对话仍可查看。',
    okText: '停用',
    okButtonProps: { danger: true },
    async onOk() {
      await digitalEmployeeApi.disableDigitalEmployee(employee.slug)
      message.success('数字员工已停用')
      await loadEmployees()
    }
  })
}

const remove = (employee) => {
  Modal.confirm({
    title: `删除草稿「${employee.name}」？`,
    content: '只有没有任何对话、定时任务或 IM Bot 引用的草稿可以删除。',
    okText: '删除',
    okButtonProps: { danger: true },
    async onOk() {
      await digitalEmployeeApi.deleteDigitalEmployee(employee.slug)
      message.success('草稿已删除')
      if (selected.value?.slug === employee.slug) detailOpen.value = false
      await loadEmployees()
    }
  })
}

onMounted(async () => {
  await loadEmployees()
  if (canCreate.value) await loadBackends()
  const editSlug = typeof route.query.edit === 'string' ? route.query.edit : ''
  if (editSlug) {
    const employee = employees.value.find((item) => item.slug === editSlug)
    if (employee) {
      openEdit(employee)
      const query = { ...route.query }
      delete query.edit
      await router.replace({ query })
    }
  }
})
</script>

<template>
  <div class="digital-employee-view">
    <PageHeader title="数字员工" :loading="loading" :show-border="true">
      <template #info>
        <div class="summary-strip">
          <span>{{ statusText }}</span>
        </div>
      </template>
    </PageHeader>

    <PageShoulder v-model:search="searchQuery" search-placeholder="搜索姓名、岗位或能力">
      <template v-if="showStatusFilter" #filters>
        <a-select v-model:value="statusFilter" :options="statusOptions" class="status-select" />
      </template>
      <template #actions>
        <a-button :loading="loading" @click="loadEmployees">
          <RefreshCw :size="15" />
          刷新
        </a-button>
        <a-button v-if="canCreate" type="primary" @click="openCreate">
          <Plus :size="15" />
          配置数字员工
        </a-button>
      </template>
    </PageShoulder>

    <div class="employee-content">
      <a-spin :spinning="loading">
        <ExtensionCardGrid v-if="filteredEmployees.length" :items="filteredEmployees">
          <InfoCard
            v-for="employee in filteredEmployees"
            :key="employee.slug"
            :title="employee.name"
            :subtitle="employeeSubtitle(employee)"
            :description="employee.profile.responsibilities || '暂未填写职责'"
            @click="openDetail(employee)"
          >
            <template #icon>
              <div class="employee-avatar"><Users :size="18" /></div>
            </template>
            <template #status>
              <span
                class="employee-status"
                :class="`employee-status--${digitalEmployeeStatusMeta(employee.profile.status).tone}`"
              >
                {{ digitalEmployeeStatusMeta(employee.profile.status).label }}
              </span>
            </template>
            <template #tags>
              <span
                v-for="capability in digitalEmployeeCapabilities(employee)"
                :key="capability"
                class="employee-capability"
              >
                {{ capability }}
              </span>
            </template>
            <template #actions>
              <button type="button" class="employee-chat-link" @click.stop="openDetail(employee)">详情</button>
              <button
                v-if="employee.profile.status === 'active'"
                type="button"
                class="employee-chat-link"
                @click.stop="goChat(employee)"
              >
                去对话
              </button>
            </template>
          </InfoCard>
        </ExtensionCardGrid>
        <div v-else-if="!loading" class="employee-empty">
          <a-empty :image="false" description="暂无数字员工" />
        </div>
      </a-spin>
    </div>

    <AgentEditModal
      ref="editModalRef"
      digital-employee
      :backend-options="backendOptions"
      @saved="handleSaved"
    />

    <a-drawer v-model:open="detailOpen" :width="520" placement="right" :title="selected?.name || '数字员工详情'">
      <template v-if="selected">
        <div class="employee-detail-head">
          <div class="employee-detail-avatar"><Users :size="24" /></div>
          <div class="employee-detail-title">
            <h2>{{ selected.name }}</h2>
            <p>{{ employeeSubtitle(selected) }}</p>
          </div>
          <span class="employee-status" :class="`employee-status--${selectedStatus.tone}`">
            {{ selectedStatus.label }}
          </span>
        </div>

        <a-descriptions :column="1" size="small" bordered class="employee-detail-meta">
          <a-descriptions-item label="共享范围">{{ selectedShareLabel }}</a-descriptions-item>
          <a-descriptions-item label="发布时间">{{ formatTime(selected.profile.published_at) }}</a-descriptions-item>
          <a-descriptions-item v-if="selected.profile.disabled_at" label="停用时间">
            {{ formatTime(selected.profile.disabled_at) }}
          </a-descriptions-item>
        </a-descriptions>

        <section class="employee-detail-section">
          <h3>职责</h3>
          <p>{{ selected.profile.responsibilities || '暂未填写' }}</p>
        </section>
        <section class="employee-detail-section">
          <h3>能力</h3>
          <div v-if="selectedCapabilities.length" class="employee-detail-tags">
            <span v-for="capability in selectedCapabilities" :key="capability" class="employee-capability">
              {{ capability }}
            </span>
          </div>
          <p v-else class="muted">暂未填写</p>
        </section>
        <section class="employee-detail-section">
          <h3>边界</h3>
          <p>{{ selected.profile.boundaries || '暂未填写' }}</p>
        </section>
        <section class="employee-detail-section">
          <h3>验收标准</h3>
          <p>{{ selected.profile.acceptance || '暂未填写' }}</p>
        </section>

        <section v-if="selected.can_manage" class="employee-detail-section">
          <h3>运行时资源</h3>
          <div class="resource-summary">
            <div v-for="item in resourceSummary" :key="item.label" class="resource-row">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
            </div>
          </div>
        </section>
        <p v-else class="employee-permission-note">运行时资源按你的权限解析，不会继承员工的私有知识库或 Skills。</p>

        <div class="employee-drawer-actions">
          <a-button v-if="selected.profile.status === 'active'" type="primary" @click="goChat(selected)">
            去对话
          </a-button>
          <span v-else-if="selected.profile.status === 'disabled'" class="employee-permission-note">
            已停用，历史对话可在会话列表中查看。
          </span>
          <span v-else class="employee-permission-note">发布后可发起对话。</span>
          <template v-if="selected.can_manage">
            <a-button @click="openEdit(selected)">编辑</a-button>
            <a-button v-if="selected.profile.status === 'draft'" type="primary" @click="publish(selected)">
              发布
            </a-button>
            <a-button v-if="selected.profile.status === 'active'" danger @click="disable(selected)">停用</a-button>
            <a-button v-if="selected.profile.status === 'draft'" danger @click="remove(selected)">
              删除草稿
            </a-button>
          </template>
        </div>
      </template>
    </a-drawer>
  </div>
</template>

<style lang="less" scoped>
.digital-employee-view {
  display: flex;
  flex-direction: column;
  min-height: 100%;
  background: var(--gray-0);
  color: var(--gray-1000);
}

.summary-strip {
  span {
    padding: 6px 10px;
    border: 1px solid var(--gray-100);
    border-radius: 7px;
    background: var(--gray-10);
    color: var(--gray-700);
    font-size: 12px;
    line-height: 18px;
  }
}

.status-select {
  width: 132px;
}

.employee-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.employee-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 72px 0;
}

.employee-avatar,
.employee-detail-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--gray-700);
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 10px;
}

.employee-avatar {
  width: 38px;
  height: 38px;
}

.employee-detail-avatar {
  width: 48px;
  height: 48px;
}

.employee-status {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;

  &--success {
    color: var(--color-success-700);
    background: var(--color-success-50);
  }

  &--warning {
    color: var(--color-warning-700);
    background: var(--color-warning-50);
  }

  &--muted {
    color: var(--gray-600);
    background: var(--gray-50);
  }
}

.employee-capability {
  display: inline-flex;
  padding: 1px 7px;
  border-radius: 5px;
  color: var(--gray-700);
  background: var(--gray-25);
  border: 1px solid var(--gray-100);
  font-size: 12px;
  line-height: 18px;
}

.employee-chat-link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--primary-color);
  font-size: 13px;
  cursor: pointer;

  & + & {
    margin-left: 10px;
  }
}

.employee-detail-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 16px;
}

.employee-detail-title {
  min-width: 0;

  h2 {
    margin: 0;
    font-size: 18px;
    line-height: 26px;
  }

  p {
    margin: 2px 0 0;
    color: var(--gray-500);
    font-size: 13px;
  }
}

.employee-detail-meta {
  margin-bottom: 18px;
}

.employee-detail-section {
  padding: 14px 0;
  border-top: 1px solid var(--gray-50);

  h3 {
    margin: 0 0 8px;
    color: var(--gray-800);
    font-size: 13px;
    font-weight: 600;
  }

  p {
    margin: 0;
    color: var(--gray-700);
    font-size: 13px;
    line-height: 22px;
    white-space: pre-wrap;
  }
}

.employee-detail-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.resource-summary {
  display: grid;
  gap: 6px;
}

.resource-row {
  display: flex;
  justify-content: space-between;
  padding: 7px 10px;
  border-radius: 7px;
  background: var(--gray-10);
  color: var(--gray-600);
  font-size: 13px;

  strong {
    color: var(--gray-900);
    font-weight: 500;
  }
}

.employee-permission-note,
.muted {
  color: var(--gray-500);
  font-size: 12px;
  line-height: 20px;
}

.employee-drawer-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 18px;
}
</style>
