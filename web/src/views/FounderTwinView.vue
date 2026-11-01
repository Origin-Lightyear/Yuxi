<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { BookOpen, ListChecks, MessageCirclePlus, Plus, RefreshCw, Video } from '@lucide/vue'

import { founderTwinApi } from '@/apis/founderTwinApi'
import MeetingWorkspace from '@/components/meeting/MeetingWorkspace.vue'
import { useUserStore } from '@/stores/user'
import {
  founderTwinCanManage,
  founderTwinEntryStatusMeta,
  founderTwinHandoffStatusMeta
} from '@/utils/founderTwin'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const errorText = ref('')
const twins = ref([])
const selectedSlug = ref('')
const selectedTwin = ref(null)
const activeTab = ref(route.query.meeting ? 'meetings' : 'entries')
const entries = ref([])
const consultations = ref([])
const handoffs = ref([])
const dataLoading = ref(false)
const dataError = ref('')

const entryModalOpen = ref(false)
const entrySaving = ref(false)
const editingEntryId = ref(null)
const entryForm = reactive({
  code: '',
  title: '',
  situation: '',
  judgment: '',
  boundary: '',
  source_type: 'manual',
  source_ref: '',
  source_excerpt: ''
})

const twinModalOpen = ref(false)
const twinSaving = ref(false)
const twinForm = reactive({ name: '', slug: '', description: '' })
const handoffDrafts = ref({})

const canManage = computed(() => founderTwinCanManage(selectedTwin.value))
const canCreateTwin = computed(() => userStore.isAdmin)
const canAnswerHandoff = (item) => Boolean(item?.can_answer)

const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN')
}

const applyTwin = (twin) => {
  selectedTwin.value = twin
  selectedSlug.value = twin?.slug || ''
  if (twin?.slug) {
    twinForm.name = twin.name || ''
    twinForm.slug = twin.slug
    twinForm.description = twin.description || ''
  }
}

const selectTwin = async (twin, { syncRoute = true } = {}) => {
  applyTwin(twin)
  if (syncRoute && route.query.twin !== twin.slug) {
    await router.replace({ query: { ...route.query, twin: twin.slug } })
  }
  await loadTwinData()
}

const loadTwinData = async () => {
  if (!selectedSlug.value) return
  dataLoading.value = true
  dataError.value = ''
  try {
    const [detail, entryResponse, consultationResponse, handoffResponse] = await Promise.all([
      founderTwinApi.getTwin(selectedSlug.value),
      founderTwinApi.listEntries(selectedSlug.value),
      founderTwinApi.listConsultations(selectedSlug.value),
      founderTwinApi.listHandoffs(selectedSlug.value)
    ])
    applyTwin(detail.twin)
    entries.value = entryResponse.entries || []
    consultations.value = consultationResponse.consultations || []
    handoffs.value = handoffResponse.handoffs || []
    handoffDrafts.value = Object.fromEntries(
      handoffs.value.map((item) => [item.id, item.answer || ''])
    )
  } catch (error) {
    dataError.value = error.message || '加载老板分身详情失败'
    entries.value = []
    consultations.value = []
    handoffs.value = []
  } finally {
    dataLoading.value = false
  }
}

const loadTwins = async () => {
  loading.value = true
  errorText.value = ''
  try {
    const response = await founderTwinApi.listTwins()
    twins.value = response.twins || []
    const requested = String(route.query.twin || '')
    const initial = twins.value.find((item) => item.slug === requested) || twins.value[0]
    if (initial) {
      applyTwin(initial)
      await loadTwinData()
    } else {
      selectedTwin.value = null
      selectedSlug.value = ''
    }
  } catch (error) {
    errorText.value = error.message || '加载老板分身列表失败'
  } finally {
    loading.value = false
  }
}

const reloadSelected = async () => {
  await loadTwins()
  if (selectedSlug.value) {
    const fresh = twins.value.find((item) => item.slug === selectedSlug.value)
    if (fresh) await selectTwin(fresh, { syncRoute: false })
  }
}

const startConsultation = () => {
  if (!selectedSlug.value) return
  router.push({ name: 'AgentComp', query: { agent_id: selectedSlug.value } })
}

const openCreateEntry = () => {
  editingEntryId.value = null
  Object.assign(entryForm, {
    code: '',
    title: '',
    situation: '',
    judgment: '',
    boundary: '',
    source_type: 'manual',
    source_ref: '',
    source_excerpt: ''
  })
  entryModalOpen.value = true
}

const openEditEntry = (entry) => {
  editingEntryId.value = entry.id
  Object.assign(entryForm, {
    code: entry.code || '',
    title: entry.title || '',
    situation: entry.situation || '',
    judgment: entry.judgment || '',
    boundary: entry.boundary || '',
    source_type: entry.source_type || 'manual',
    source_ref: entry.source_ref || '',
    source_excerpt: entry.source_excerpt || ''
  })
  entryModalOpen.value = true
}

const saveEntry = async () => {
  if (!entryForm.title.trim() || !entryForm.judgment.trim()) {
    message.warning('请填写经验标题和判断')
    return
  }
  entrySaving.value = true
  try {
    const payload = {
      code: entryForm.code.trim() || undefined,
      title: entryForm.title.trim(),
      situation: entryForm.situation.trim(),
      judgment: entryForm.judgment.trim(),
      boundary: entryForm.boundary.trim(),
      source_type: entryForm.source_type,
      source_ref: entryForm.source_ref.trim() || null,
      source_excerpt: entryForm.source_excerpt.trim() || null
    }
    if (editingEntryId.value) {
      await founderTwinApi.updateEntry(selectedSlug.value, editingEntryId.value, payload)
      message.success('经验已保存，需重新确认后才能被引用')
    } else {
      await founderTwinApi.createEntry(selectedSlug.value, payload)
      message.success('经验草稿已创建')
    }
    entryModalOpen.value = false
    await loadTwinData()
  } catch (error) {
    message.error(error.message || '保存经验失败')
  } finally {
    entrySaving.value = false
  }
}

const confirmEntry = (entry) => {
  Modal.confirm({
    title: `确认「${entry.title}」？`,
    content: '确认后会生成检索向量，分身才能引用这条经验。',
    okText: '确认',
    async onOk() {
      try {
        await founderTwinApi.confirmEntry(selectedSlug.value, entry.id)
        message.success('经验已确认')
        await loadTwinData()
      } catch (error) {
        message.error(error.message || '确认失败：embedding 未就绪或服务不可用')
      }
    }
  })
}

const invalidateEntry = (entry) => {
  Modal.confirm({
    title: `失效「${entry.title}」？`,
    content: '失效后不会再被检索和引用，历史咨询记录仍保留。',
    okText: '失效',
    okButtonProps: { danger: true },
    async onOk() {
      try {
        await founderTwinApi.invalidateEntry(selectedSlug.value, entry.id)
        message.success('经验已失效')
        await loadTwinData()
      } catch (error) {
        message.error(error.message || '失效经验失败')
      }
    }
  })
}

const answerHandoff = async (handoff, closeOnly = false) => {
  const answer = String(handoffDrafts.value[handoff.id] || '').trim()
  if (!closeOnly && !answer) {
    message.warning('请填写回答')
    return
  }
  try {
    await founderTwinApi.updateHandoff(selectedSlug.value, handoff.id, {
      ...(closeOnly ? { status: 'closed' } : { answer })
    })
    message.success(closeOnly ? '转交单已关闭' : '回答已保存')
    await loadTwinData()
  } catch (error) {
    message.error(error.message || '处理转交单失败')
  }
}

const openCreateTwin = () => {
  Object.assign(twinForm, { name: '', slug: '', description: '' })
  twinModalOpen.value = true
}

const createTwin = async () => {
  if (!twinForm.name.trim()) {
    message.warning('请填写分身名称')
    return
  }
  twinSaving.value = true
  try {
    const response = await founderTwinApi.createTwin({
      name: twinForm.name.trim(),
      slug: twinForm.slug.trim() || undefined,
      description: twinForm.description.trim() || undefined
    })
    twinModalOpen.value = false
    message.success('老板分身已创建')
    twins.value = []
    await loadTwins()
    await selectTwin(response.twin)
  } catch (error) {
    message.error(error.message || '创建老板分身失败')
  } finally {
    twinSaving.value = false
  }
}

watch(
  () => route.query.twin,
  (slug) => {
    if (!slug || slug === selectedSlug.value) return
    const target = twins.value.find((item) => item.slug === slug)
    if (target) {
      applyTwin(target)
      loadTwinData()
    }
  }
)

watch(
  () => route.query.meeting,
  (meetingId) => {
    if (meetingId) activeTab.value = 'meetings'
  }
)

onMounted(loadTwins)
</script>

<template>
  <div class="founder-twin-page">
    <aside class="twin-list">
      <header class="list-header">
        <div>
          <p class="eyebrow">企业判断分身</p>
          <h1>老板分身</h1>
        </div>
        <a-button v-if="canCreateTwin" type="text" size="small" @click="openCreateTwin">
          <Plus :size="15" />
          新建
        </a-button>
      </header>

      <a-alert
        v-if="errorText"
        type="error"
        show-icon
        :message="errorText"
        class="list-alert"
      >
        <template #action>
          <a-button size="small" type="text" @click="loadTwins">重试</a-button>
        </template>
      </a-alert>

      <a-spin :spinning="loading">
        <a-empty v-if="!loading && !twins.length" description="还没有可用的老板分身" />
        <div v-else class="twin-cards">
          <button
            v-for="twin in twins"
            :key="twin.slug"
            type="button"
            class="twin-card"
            :class="{ active: twin.slug === selectedSlug }"
            @click="selectTwin(twin)"
          >
            <span class="twin-name">{{ twin.name }}</span>
            <span class="twin-desc">{{ twin.description || '尚未填写分身说明' }}</span>
            <span class="twin-stats">
              {{ twin.stats?.confirmed_entries || 0 }} 条已确认经验
            </span>
          </button>
        </div>
      </a-spin>
    </aside>

    <main class="twin-detail">
      <template v-if="selectedTwin">
        <header class="detail-header">
          <div>
            <p class="eyebrow">老板分身</p>
            <h2>{{ selectedTwin.name }}</h2>
            <p class="detail-desc">{{ selectedTwin.description || '尚未填写分身说明' }}</p>
          </div>
          <div class="detail-actions">
            <a-button :loading="loading || dataLoading" @click="reloadSelected">
              <RefreshCw :size="15" />
              刷新
            </a-button>
            <a-button type="primary" @click="startConsultation">
              <MessageCirclePlus :size="15" />
              开始咨询
            </a-button>
          </div>
        </header>

        <a-alert
          v-if="dataError"
          type="error"
          show-icon
          :message="dataError"
          class="data-alert"
        >
          <template #action>
            <a-button size="small" type="text" @click="loadTwinData">重试</a-button>
          </template>
        </a-alert>

        <a-spin :spinning="dataLoading">
          <a-tabs v-model:active-key="activeTab" class="twin-tabs">
            <a-tab-pane key="entries">
              <template #tab>
                <span class="tab-label"><BookOpen :size="15" />经验库</span>
              </template>
              <section class="pane">
                <div class="pane-toolbar">
                  <span class="pane-hint">
                    {{ canManage ? '确认后的经验才能被分身引用' : '只展示已确认经验' }}
                  </span>
                  <a-button v-if="canManage" type="primary" size="small" @click="openCreateEntry">
                    <Plus :size="14" />
                    新建经验
                  </a-button>
                </div>
                <a-empty v-if="!entries.length" description="还没有经验条目" />
                <article v-for="entry in entries" :key="entry.id" class="entry-card">
                  <div class="entry-head">
                    <div>
                      <div class="entry-title-row">
                        <h3>{{ entry.title }}</h3>
                        <a-tag :color="founderTwinEntryStatusMeta(entry.status).color">
                          {{ founderTwinEntryStatusMeta(entry.status).label }}
                        </a-tag>
                      </div>
                      <p class="entry-meta">
                        {{ entry.code }} · 引用 {{ entry.citation_count || 0 }} 次
                      </p>
                    </div>
                    <div v-if="canManage" class="entry-actions">
                      <a-button
                        v-if="entry.status !== 'invalid'"
                        size="small"
                        type="text"
                        @click="openEditEntry(entry)"
                      >
                        编辑
                      </a-button>
                      <a-button
                        v-if="entry.status === 'draft'"
                        size="small"
                        type="text"
                        @click="confirmEntry(entry)"
                      >
                        确认
                      </a-button>
                      <a-button
                        v-if="entry.status !== 'invalid'"
                        size="small"
                        type="text"
                        danger
                        @click="invalidateEntry(entry)"
                      >
                        失效
                      </a-button>
                    </div>
                  </div>
                  <dl class="entry-grid">
                    <div>
                      <dt>场景</dt>
                      <dd>{{ entry.situation || '—' }}</dd>
                    </div>
                    <div>
                      <dt>判断</dt>
                      <dd>{{ entry.judgment }}</dd>
                    </div>
                    <div>
                      <dt>边界</dt>
                      <dd>{{ entry.boundary || '—' }}</dd>
                    </div>
                  </dl>
                </article>
              </section>
            </a-tab-pane>

            <a-tab-pane key="meetings">
              <template #tab>
                <span class="tab-label"><Video :size="15" />分身会议</span>
              </template>
              <MeetingWorkspace kind="twin" embedded />
            </a-tab-pane>

            <a-tab-pane key="consultations">
              <template #tab>
                <span class="tab-label"><MessageCirclePlus :size="15" />咨询记录</span>
              </template>
              <section class="pane">
                <a-empty v-if="!consultations.length" description="还没有咨询记录" />
                <article v-for="item in consultations" :key="item.id" class="record-card">
                  <p class="record-question">{{ item.question || '未记录问题' }}</p>
                  <p class="record-answer">{{ item.answer || '尚无答案' }}</p>
                  <p class="record-meta">
                    引用 {{ (item.cited_entry_ids || []).length }} 条经验
                    <template v-if="item.confidence !== null"> · 置信度 {{ item.confidence }}</template>
                    · {{ formatTime(item.created_at) }}
                  </p>
                  <div v-if="item.cited_entries?.length" class="citation-list">
                    <span>依据：</span>
                    <a-tag v-for="entry in item.cited_entries" :key="entry.id">
                      {{ entry.title }}
                    </a-tag>
                  </div>
                  <div v-if="item.external_citations?.length" class="citation-list">
                    <span>其他分口径（本人未复核）：</span>
                    <a-tag
                      v-for="citation in item.external_citations"
                      :key="`${citation.twin_slug}-${citation.entry_id}`"
                      color="orange"
                    >
                      {{ citation.twin_name }} · {{ citation.title || `经验 #${citation.entry_id}` }}
                    </a-tag>
                  </div>
                </article>
              </section>
            </a-tab-pane>

            <a-tab-pane key="handoffs">
              <template #tab>
                <span class="tab-label"><ListChecks :size="15" />转交</span>
              </template>
              <section class="pane">
                <a-empty v-if="!handoffs.length" description="没有需要老板回答的问题" />
                <article v-for="item in handoffs" :key="item.id" class="record-card">
                  <div class="record-head">
                    <p class="record-question">{{ item.question }}</p>
                    <a-tag :color="founderTwinHandoffStatusMeta(item.status).color">
                      {{ founderTwinHandoffStatusMeta(item.status).label }}
                    </a-tag>
                  </div>
                  <p class="record-meta">
                    发起人 {{ item.requester_name || '员工' }}
                    <template v-if="item.assignee_name"> · 指派给 {{ item.assignee_name }}</template>
                    · {{ formatTime(item.created_at) }}
                  </p>
                  <p v-if="item.answer" class="record-answer">{{ item.answer }}</p>
                  <div v-if="canAnswerHandoff(item) && item.status !== 'closed'" class="handoff-form">
                    <a-textarea
                      v-model:value="handoffDrafts[item.id]"
                      :rows="3"
                      placeholder="写下老板本人的回答，保存后会标记为已回答"
                    />
                    <div class="handoff-actions">
                      <a-button size="small" @click="answerHandoff(item, true)">关闭</a-button>
                      <a-button size="small" type="primary" @click="answerHandoff(item)">
                        保存回答
                      </a-button>
                    </div>
                  </div>
                </article>
              </section>
            </a-tab-pane>
          </a-tabs>
        </a-spin>
      </template>

      <MeetingWorkspace v-else-if="route.query.meeting" kind="twin" embedded />
      <a-empty v-else description="请选择一个老板分身" />
    </main>

    <a-modal
      v-model:open="entryModalOpen"
      :title="editingEntryId ? '编辑经验' : '新建经验'"
      :confirm-loading="entrySaving"
      ok-text="保存"
      cancel-text="取消"
      width="640px"
      @ok="saveEntry"
    >
      <a-form layout="vertical">
        <a-form-item label="标题" required>
          <a-input v-model:value="entryForm.title" maxlength="200" />
        </a-form-item>
        <a-form-item label="场景">
          <a-textarea v-model:value="entryForm.situation" :rows="2" />
        </a-form-item>
        <a-form-item label="判断" required>
          <a-textarea v-model:value="entryForm.judgment" :rows="3" />
        </a-form-item>
        <a-form-item label="边界">
          <a-textarea v-model:value="entryForm.boundary" :rows="2" />
        </a-form-item>
        <a-form-item label="来源类型">
          <a-input v-model:value="entryForm.source_type" placeholder="manual" />
        </a-form-item>
        <a-form-item label="来源引用">
          <a-input v-model:value="entryForm.source_ref" placeholder="可选，文件或链接" />
        </a-form-item>
        <a-form-item label="来源摘录">
          <a-textarea v-model:value="entryForm.source_excerpt" :rows="2" />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="twinModalOpen"
      title="新建老板分身"
      :confirm-loading="twinSaving"
      ok-text="创建"
      cancel-text="取消"
      @ok="createTwin"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="twinForm.name" maxlength="100" />
        </a-form-item>
        <a-form-item label="Slug">
          <a-input v-model:value="twinForm.slug" placeholder="可选，例如 boss" />
        </a-form-item>
        <a-form-item label="说明">
          <a-textarea v-model:value="twinForm.description" :rows="3" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
.founder-twin-page {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  height: 100%;
  background: var(--gray-0);
}

.twin-list {
  border-right: 1px solid var(--gray-200);
  padding: 22px 16px;
  overflow-y: auto;
  background: var(--gray-25);
}

.list-header,
.detail-header,
.pane-toolbar,
.entry-head,
.record-head,
.handoff-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.list-header h1,
.detail-header h2 {
  margin: 2px 0 0;
}

.eyebrow {
  margin: 0;
  font-size: 11px;
  letter-spacing: 0.12em;
  color: var(--gray-500);
  text-transform: uppercase;
}

.twin-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
}

.twin-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 14px;
  text-align: left;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  background: var(--gray-0);
  cursor: pointer;
}

.twin-card.active {
  border-color: var(--primary-color);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary-color) 12%, transparent);
}

.twin-name {
  font-weight: 600;
  color: var(--gray-900);
}

.twin-desc {
  font-size: 12px;
  color: var(--gray-600);
  line-height: 1.5;
}

.twin-stats {
  font-size: 11px;
  color: var(--gray-500);
}

.twin-detail {
  padding: 24px 28px;
  overflow-y: auto;
}

.detail-desc {
  margin: 8px 0 0;
  color: var(--gray-600);
}

.detail-actions {
  display: flex;
  gap: 8px;
}

.data-alert,
.list-alert {
  margin-top: 16px;
}

.twin-tabs {
  margin-top: 18px;
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.pane {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 8px;
}

.pane-hint,
.entry-meta,
.record-meta {
  font-size: 12px;
  color: var(--gray-500);
}

.entry-card,
.record-card {
  padding: 16px;
  border: 1px solid var(--gray-200);
  border-radius: 12px;
  background: var(--gray-0);
}

.entry-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.entry-title-row h3 {
  margin: 0;
  font-size: 15px;
}

.entry-meta {
  margin: 4px 0 0;
}

.entry-actions {
  display: flex;
  gap: 2px;
}

.entry-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 14px 0 0;
}

.entry-grid dt {
  font-size: 11px;
  color: var(--gray-500);
}

.entry-grid dd {
  margin: 4px 0 0;
  white-space: pre-wrap;
  line-height: 1.6;
}

.record-question {
  margin: 0;
  font-weight: 600;
}

.record-answer {
  margin: 10px 0 0;
  white-space: pre-wrap;
  line-height: 1.7;
}

.record-meta {
  margin: 8px 0 0;
}

.citation-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--gray-500);
}

.handoff-form {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.handoff-actions {
  justify-content: flex-end;
}

@media (max-width: 900px) {
  .founder-twin-page {
    grid-template-columns: 1fr;
  }

  .twin-list {
    border-right: none;
    border-bottom: 1px solid var(--gray-200);
    max-height: 260px;
  }

  .entry-grid {
    grid-template-columns: 1fr;
  }
}
</style>
