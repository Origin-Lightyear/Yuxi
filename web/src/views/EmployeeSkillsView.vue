<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { Award, Plus, RefreshCw } from '@lucide/vue'

import PageHeader from '@/components/shared/PageHeader.vue'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import { employeeSkillApi } from '@/apis/employeeSkillApi'
import { useUserStore } from '@/stores/user'
import {
  captureMethodMeta,
  employeeSkillStatusMeta,
  handoverStatusMeta,
  splitSkillLines
} from '@/utils/employeeSkill'

const userStore = useUserStore()

const loading = ref(false)
const errorText = ref('')
const skills = ref([])
const searchQuery = ref('')
const statusFilter = ref('')
const handoverFilter = ref('')
const viewMode = ref('list')

const detailOpen = ref(false)
const selected = ref(null)

const formOpen = ref(false)
const formSaving = ref(false)
const editingId = ref(null)
const form = reactive({
  title: '',
  department: '',
  capture_method: 'interview',
  stepsText: '',
  rulesText: '',
  boundary: '',
  acceptance: '',
  source_ref: ''
})

const handoverOpen = ref(false)
const handoverSaving = ref(false)
const handoverSearch = ref('')
const handoverCandidates = ref([])
const handoverTarget = ref('')

const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'draft', label: '草稿' },
  { value: 'needs_review', label: '待确认' },
  { value: 'ready', label: '已就绪' },
  { value: 'archived', label: '已归档' }
]
const handoverOptions = [
  { value: '', label: '全部交接状态' },
  { value: 'unassigned', label: '未分配' },
  { value: 'assigned', label: '已指派' },
  { value: 'accepted', label: '已接手' }
]
const captureOptions = [
  { value: 'interview', label: '访谈' },
  { value: 'recording', label: '录音' },
  { value: 'process', label: '流程' },
  { value: 'review', label: '复盘' }
]

const summaryCards = computed(() => [
  {
    key: 'ready',
    label: '已就绪',
    value: skills.value.filter((item) => item.status === 'ready').length
  },
  {
    key: 'review',
    label: '待确认',
    value: skills.value.filter((item) => ['draft', 'needs_review'].includes(item.status)).length
  },
  {
    key: 'assigned',
    label: '已指派',
    value: skills.value.filter((item) => item.handover_status === 'assigned').length
  },
  {
    key: 'accepted',
    label: '已接手',
    value: skills.value.filter((item) => item.handover_status === 'accepted').length
  }
])

const filteredSkills = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase()
  return skills.value.filter((item) => {
    if (statusFilter.value && item.status !== statusFilter.value) return false
    if (handoverFilter.value && item.handover_status !== handoverFilter.value) return false
    if (!keyword) return true
    return [item.title, item.department, item.code, item.boundary, ...(item.steps || [])]
      .filter(Boolean)
      .join(' ')
      .toLowerCase()
      .includes(keyword)
  })
})

const boardColumns = computed(() =>
  ['unassigned', 'assigned', 'accepted'].map((key) => ({
    key,
    label: handoverStatusMeta(key).label,
    items: filteredSkills.value.filter((item) => item.handover_status === key)
  }))
)

const canAcceptSelected = computed(
  () =>
    selected.value?.handover_status === 'assigned' &&
    (userStore.isAdmin || selected.value?.successor_uid === userStore.uid)
)

const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN')
}

const loadSkills = async () => {
  loading.value = true
  errorText.value = ''
  try {
    const response = await employeeSkillApi.listSkills()
    skills.value = response.skills || []
    if (selected.value) {
      selected.value = skills.value.find((item) => item.id === selected.value.id) || null
    }
  } catch (error) {
    errorText.value = error.message || '加载技能卡失败'
    skills.value = []
  } finally {
    loading.value = false
  }
}

const openDetail = (skill) => {
  selected.value = skill
  detailOpen.value = true
}

const resetForm = () => {
  form.title = ''
  form.department = ''
  form.capture_method = 'interview'
  form.stepsText = ''
  form.rulesText = ''
  form.boundary = ''
  form.acceptance = ''
  form.source_ref = ''
}

const openCreate = () => {
  editingId.value = null
  resetForm()
  formOpen.value = true
}

const openEdit = (skill) => {
  editingId.value = skill.id
  form.title = skill.title || ''
  form.department = skill.department || ''
  form.capture_method = skill.capture_method || 'interview'
  form.stepsText = (skill.steps || []).join('\n')
  form.rulesText = (skill.rules || []).join('\n')
  form.boundary = skill.boundary || ''
  form.acceptance = skill.acceptance || ''
  form.source_ref = skill.source_ref || ''
  formOpen.value = true
}

const saveSkill = async () => {
  if (!form.title.trim()) {
    message.warning('请填写技能标题')
    return
  }
  formSaving.value = true
  const payload = {
    title: form.title.trim(),
    department: form.department.trim(),
    capture_method: form.capture_method,
    steps: splitSkillLines(form.stepsText),
    rules: splitSkillLines(form.rulesText),
    boundary: form.boundary.trim(),
    acceptance: form.acceptance.trim(),
    source_ref: form.source_ref.trim() || null
  }
  try {
    if (editingId.value) {
      const response = await employeeSkillApi.updateSkill(editingId.value, payload)
      selected.value = response.skill
    } else {
      const response = await employeeSkillApi.createSkill(payload)
      selected.value = response.skill
    }
    formOpen.value = false
    await loadSkills()
    message.success(editingId.value ? '技能卡已更新' : '技能草稿已创建')
  } catch (error) {
    message.error(error.message || '保存技能卡失败')
  } finally {
    formSaving.value = false
  }
}

const runAction = async (action, successText) => {
  if (!selected.value) return
  try {
    const response = await action(selected.value.id)
    selected.value = response.skill
    await loadSkills()
    message.success(successText)
  } catch (error) {
    message.error(error.message || '操作失败')
  }
}

const submitSkill = () => runAction(employeeSkillApi.submitSkill, '已送入待确认')
const verifySkill = () => runAction(employeeSkillApi.verifySkill, '技能已确认就绪')
const archiveSkill = () => runAction(employeeSkillApi.archiveSkill, '技能已归档')

const deleteSkill = () => {
  if (!selected.value) return
  Modal.confirm({
    title: '删除技能草稿？',
    content: '删除后不能恢复；已进入交接的技能不能删除。',
    okText: '删除',
    okType: 'danger',
    async onOk() {
      try {
        await employeeSkillApi.deleteSkill(selected.value.id)
        detailOpen.value = false
        selected.value = null
        await loadSkills()
        message.success('技能草稿已删除')
      } catch (error) {
        message.error(error.message || '删除技能失败')
      }
    }
  })
}

const openHandover = async () => {
  if (!selected.value) return
  handoverTarget.value = ''
  handoverSearch.value = ''
  handoverOpen.value = true
  await loadHandoverCandidates()
}

const loadHandoverCandidates = async () => {
  if (!selected.value) return
  try {
    const response = await employeeSkillApi.listHandoverCandidates(
      selected.value.id,
      handoverSearch.value.trim()
    )
    handoverCandidates.value = (response.users || []).map((item) => ({
      value: item.uid,
      label: item.name || item.uid
    }))
  } catch (error) {
    handoverCandidates.value = []
    message.error(error.message || '加载接手人失败')
  }
}

const assignHandover = async () => {
  if (!selected.value || !handoverTarget.value) {
    message.warning('请选择接手人')
    return
  }
  handoverSaving.value = true
  try {
    const response = await employeeSkillApi.assignHandover(selected.value.id, handoverTarget.value)
    selected.value = response.skill
    handoverOpen.value = false
    await loadSkills()
    message.success('已指派接手人')
  } catch (error) {
    message.error(error.message || '指派接手人失败')
  } finally {
    handoverSaving.value = false
  }
}

const acceptHandover = () => runAction(employeeSkillApi.acceptHandover, '已确认接手技能')

onMounted(loadSkills)
</script>

<template>
  <div class="employee-skills-page">
    <PageHeader title="技能管理" :loading="loading" :show-border="true">
      <template #info>
        <span class="head-info">把员工经验沉淀成可交接、可验收的技能卡</span>
      </template>
      <template #actions>
        <a-button :loading="loading" @click="loadSkills">
          <RefreshCw :size="15" />
          刷新
        </a-button>
        <a-button type="primary" @click="openCreate">
          <Plus :size="15" />
          新建技能
        </a-button>
      </template>
    </PageHeader>

    <PageShoulder v-model:search="searchQuery" search-placeholder="搜索技能、部门或步骤">
      <template #filters>
        <a-select v-model:value="statusFilter" :options="statusOptions" class="filter-select" />
        <a-select v-model:value="handoverFilter" :options="handoverOptions" class="filter-select" />
        <a-radio-group v-model:value="viewMode" button-style="solid" size="small">
          <a-radio-button value="list">列表</a-radio-button>
          <a-radio-button value="board">交接看板</a-radio-button>
        </a-radio-group>
      </template>
      <template #actions>
        <span class="list-count">共 {{ filteredSkills.length }} 张技能卡</span>
      </template>
    </PageShoulder>

    <main class="skills-body">
      <section class="summary-grid" aria-label="技能汇总">
        <div v-for="card in summaryCards" :key="card.key" class="summary-card">
          <span class="summary-label">{{ card.label }}</span>
          <strong class="summary-value">{{ card.value }}</strong>
        </div>
      </section>

      <a-alert v-if="errorText" type="error" show-icon :message="errorText" class="error-alert">
        <template #action>
          <a-button size="small" type="text" @click="loadSkills">重试</a-button>
        </template>
      </a-alert>

      <a-spin :spinning="loading">
        <div v-if="!loading && !filteredSkills.length" class="empty-state">
          <Award :size="34" />
          <p>还没有技能卡</p>
          <span>建议先从最依赖老员工的流程开始沉淀。</span>
          <a-button type="primary" @click="openCreate">新建第一张技能卡</a-button>
        </div>

        <div v-else-if="viewMode === 'list'" class="skill-grid">
          <article
            v-for="skill in filteredSkills"
            :key="skill.id"
            class="skill-card"
            tabindex="0"
            @click="openDetail(skill)"
            @keydown.enter="openDetail(skill)"
          >
            <div class="skill-card-head">
              <div>
                <h2>{{ skill.title }}</h2>
                <p>{{ skill.department || '未填写部门' }} · {{ skill.owner_name }}</p>
              </div>
              <a-tag :color="employeeSkillStatusMeta(skill.status).color">
                {{ employeeSkillStatusMeta(skill.status).label }}
              </a-tag>
            </div>
            <p class="skill-boundary">{{ skill.boundary || '尚未填写边界' }}</p>
            <div class="skill-steps">
              <span v-for="step in (skill.steps || []).slice(0, 3)" :key="step">{{ step }}</span>
              <span v-if="(skill.steps || []).length > 3">+{{ skill.steps.length - 3 }}</span>
            </div>
            <footer class="skill-card-foot">
              <span>{{ captureMethodMeta(skill.capture_method).label }}沉淀</span>
              <a-tag :color="handoverStatusMeta(skill.handover_status).color">
                {{ handoverStatusMeta(skill.handover_status).label }}
              </a-tag>
            </footer>
          </article>
        </div>

        <div v-else class="handover-board">
          <section v-for="column in boardColumns" :key="column.key" class="board-column">
            <header>
              <span>{{ column.label }}</span>
              <strong>{{ column.items.length }}</strong>
            </header>
            <article
              v-for="skill in column.items"
              :key="skill.id"
              class="board-card"
              @click="openDetail(skill)"
            >
              <h3>{{ skill.title }}</h3>
              <p>{{ skill.owner_name }}<template v-if="skill.successor_name"> → {{ skill.successor_name }}</template></p>
            </article>
            <a-empty v-if="!column.items.length" description="暂无技能" />
          </section>
        </div>
      </a-spin>
    </main>

    <a-drawer v-model:open="detailOpen" width="560" title="技能卡详情">
      <template v-if="selected">
        <div class="detail-head">
          <div>
            <h2>{{ selected.title }}</h2>
            <p class="detail-meta">
              {{ selected.code }} · {{ selected.department || '未填写部门' }} · {{ selected.owner_name }}
            </p>
          </div>
          <a-tag :color="employeeSkillStatusMeta(selected.status).color">
            {{ employeeSkillStatusMeta(selected.status).label }}
          </a-tag>
        </div>

        <div class="detail-tags">
          <a-tag :color="captureMethodMeta(selected.capture_method).color">
            {{ captureMethodMeta(selected.capture_method).label }}
          </a-tag>
          <a-tag :color="handoverStatusMeta(selected.handover_status).color">
            {{ handoverStatusMeta(selected.handover_status).label }}
          </a-tag>
          <span v-if="selected.successor_name">接手人：{{ selected.successor_name }}</span>
        </div>

        <section class="detail-section">
          <h3>操作步骤</h3>
          <ol>
            <li v-for="step in selected.steps" :key="step">{{ step }}</li>
          </ol>
          <p v-if="!selected.steps?.length" class="muted">尚未填写步骤</p>
        </section>

        <section class="detail-section">
          <h3>判断规则</h3>
          <ul>
            <li v-for="rule in selected.rules" :key="rule">{{ rule }}</li>
          </ul>
          <p v-if="!selected.rules?.length" class="muted">尚未填写规则</p>
        </section>

        <section class="detail-section">
          <h3>边界</h3>
          <p>{{ selected.boundary || '尚未填写边界' }}</p>
        </section>

        <section class="detail-section">
          <h3>验收标准</h3>
          <p>{{ selected.acceptance || '尚未填写验收标准' }}</p>
        </section>

        <p class="detail-time">更新于 {{ formatTime(selected.updated_at) }}</p>

        <div class="detail-actions">
          <template v-if="selected.can_manage">
            <a-button @click="openEdit(selected)">编辑</a-button>
            <a-button
              v-if="['draft', 'needs_review'].includes(selected.status)"
              @click="submitSkill"
            >
              送审
            </a-button>
            <a-button
              v-if="['draft', 'needs_review'].includes(selected.status)"
              type="primary"
              @click="verifySkill"
            >
              确认就绪
            </a-button>
            <a-button
              v-if="selected.status === 'ready' && selected.handover_status === 'unassigned'"
              @click="openHandover"
            >
              指派接手人
            </a-button>
            <a-button v-if="selected.status !== 'archived'" @click="archiveSkill">归档</a-button>
            <a-button
              v-if="selected.status === 'draft' && selected.handover_status === 'unassigned'"
              danger
              @click="deleteSkill"
            >
              删除
            </a-button>
          </template>
          <a-button v-if="canAcceptSelected" type="primary" @click="acceptHandover">
            确认接手
          </a-button>
        </div>
      </template>
    </a-drawer>

    <a-modal
      v-model:open="formOpen"
      :title="editingId ? '编辑技能卡' : '新建技能卡'"
      :confirm-loading="formSaving"
      width="680"
      ok-text="保存"
      @ok="saveSkill"
    >
      <a-form layout="vertical">
        <a-form-item label="技能标题" required>
          <a-input v-model:value="form.title" :maxlength="200" placeholder="例如：大客户报价复盘" />
        </a-form-item>
        <a-form-item label="所属部门">
          <a-input v-model:value="form.department" :maxlength="80" placeholder="例如：销售部" />
        </a-form-item>
        <a-form-item label="沉淀方式">
          <a-select v-model:value="form.capture_method" :options="captureOptions" />
        </a-form-item>
        <a-form-item label="操作步骤（一行一步）">
          <a-textarea v-model:value="form.stepsText" :rows="4" placeholder="确认客户预算&#10;核对成本&#10;给出折扣区间" />
        </a-form-item>
        <a-form-item label="判断规则（一行一条）">
          <a-textarea v-model:value="form.rulesText" :rows="3" placeholder="超过十万元必须二次确认" />
        </a-form-item>
        <a-form-item label="能力边界">
          <a-textarea v-model:value="form.boundary" :rows="3" placeholder="哪些情况不能照做，必须升级给谁" />
        </a-form-item>
        <a-form-item label="验收标准">
          <a-textarea v-model:value="form.acceptance" :rows="3" placeholder="怎样算做对了" />
        </a-form-item>
        <a-form-item label="来源引用">
          <a-input v-model:value="form.source_ref" :maxlength="512" placeholder="可选：录音、文档或会议链接" />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="handoverOpen"
      title="指派技能接手人"
      :confirm-loading="handoverSaving"
      ok-text="指派"
      @ok="assignHandover"
    >
      <a-form layout="vertical">
        <a-form-item label="搜索接手人">
          <a-input
            v-model:value="handoverSearch"
            placeholder="按姓名或 UID 搜索"
            @press-enter="loadHandoverCandidates"
          />
        </a-form-item>
        <a-form-item label="接手人" required>
          <a-select
            v-model:value="handoverTarget"
            :options="handoverCandidates"
            show-search
            placeholder="选择接手人"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.employee-skills-page {
  min-height: 100vh;
  background: var(--gray-25);
}

.head-info,
.list-count {
  color: var(--gray-500);
  font-size: 13px;
}

.filter-select {
  min-width: 132px;
}

.skills-body {
  padding: 16px var(--page-padding) 40px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.summary-card {
  padding: 14px 16px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
}

.summary-label {
  display: block;
  color: var(--gray-500);
  font-size: 12px;
}

.summary-value {
  display: block;
  margin-top: 4px;
  color: var(--gray-900);
  font-size: 24px;
}

.error-alert {
  margin-top: 14px;
}

.empty-state {
  display: flex;
  min-height: 360px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--gray-500);
}

.empty-state p {
  margin: 0;
  color: var(--gray-900);
  font-size: 16px;
}

.skill-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 14px;
  margin-top: 14px;
}

.skill-card {
  display: flex;
  min-height: 210px;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border: 1px solid var(--gray-150);
  border-radius: 14px;
  background: var(--gray-0);
  cursor: pointer;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}

.skill-card:hover {
  border-color: color-mix(in srgb, var(--main-color) 30%, var(--gray-150));
  box-shadow: 0 8px 22px color-mix(in srgb, var(--gray-2000) 8%, transparent);
}

.skill-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.skill-card h2 {
  margin: 0;
  color: var(--gray-900);
  font-size: 16px;
}

.skill-card-head p,
.skill-card-foot,
.skill-boundary,
.detail-meta,
.detail-time,
.muted {
  margin: 6px 0 0;
  color: var(--gray-500);
  font-size: 12px;
}

.skill-boundary {
  color: var(--gray-700);
  font-size: 13px;
  line-height: 1.6;
}

.skill-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.skill-steps span {
  padding: 4px 8px;
  border-radius: 999px;
  background: var(--gray-50);
  color: var(--gray-600);
  font-size: 12px;
}

.skill-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
}

.handover-board {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-top: 14px;
}

.board-column {
  min-height: 280px;
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 14px;
  background: var(--gray-0);
}

.board-column header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  color: var(--gray-700);
  font-weight: 600;
}

.board-card {
  padding: 12px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-25);
  cursor: pointer;
}

.board-card + .board-card {
  margin-top: 8px;
}

.board-card h3 {
  margin: 0;
  color: var(--gray-900);
  font-size: 14px;
}

.board-card p {
  margin: 6px 0 0;
  color: var(--gray-500);
  font-size: 12px;
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.detail-head h2 {
  margin: 0;
  color: var(--gray-900);
}

.detail-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  color: var(--gray-500);
  font-size: 12px;
}

.detail-section {
  margin-top: 20px;
}

.detail-section h3 {
  margin: 0 0 8px;
  color: var(--gray-900);
  font-size: 14px;
}

.detail-section p,
.detail-section li {
  color: var(--gray-700);
  line-height: 1.7;
}

.detail-section ol,
.detail-section ul {
  margin: 0;
  padding-left: 20px;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--gray-150);
}

@media (max-width: 900px) {
  .summary-grid,
  .handover-board {
    grid-template-columns: 1fr;
  }
}
</style>
