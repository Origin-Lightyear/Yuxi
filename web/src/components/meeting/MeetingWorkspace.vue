<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { Check, Play, Plus, RefreshCw, Send, X } from '@lucide/vue'

import { digitalEmployeeApi } from '@/apis/digital_employee_api'
import { founderTwinApi } from '@/apis/founderTwinApi'
import { meetingApi } from '@/apis/meetingApi'
import { useUserStore } from '@/stores/user'
import {
  decisionStatusMeta,
  meetingCanJoin,
  meetingCanRun,
  meetingSpeakerName,
  meetingStatusMeta,
  signerStatusMeta,
  speakerTypeLabel
} from '@/utils/meeting'

const props = defineProps({
  kind: { type: String, default: 'twin' },
  embedded: { type: Boolean, default: false }
})

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const detailLoading = ref(false)
const errorText = ref('')
const meetings = ref([])
const selected = ref(null)
const participantOptions = ref([])

const createOpen = ref(false)
const creating = ref(false)
const form = reactive({ topic: '', participant_slugs: [], rounds: 2 })
const running = ref(false)
const messageText = ref('')
const messageSending = ref(false)

const signOpen = ref(false)
const signing = ref(false)
const signForm = reactive({ decision: null, action: 'approve', comment: '', text: '' })

const isTwin = computed(() => props.kind === 'twin')
const title = computed(() => (isTwin.value ? '分身会议' : 'AI 会议'))
const eyebrow = computed(() => (isTwin.value ? '分身协作' : '数字员工协作'))
const canCreate = computed(
  () => userStore.isAdmin || participantOptions.value.some((item) => item.canManage)
)
const selectedPendingCount = computed(() => selected.value?.pending_decision_count || 0)

const participantName = (slug) => {
  const participant = selected.value?.participants?.find((item) => item.slug === slug)
  return participant?.name || slug || '未知参与者'
}

const formatTime = (value) => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN')
}

const loadParticipantOptions = async () => {
  try {
    if (isTwin.value) {
      const response = await founderTwinApi.listTwins()
      participantOptions.value = (response.twins || []).map((item) => ({
        value: item.slug,
        label: item.name,
        description: item.description || '',
        canManage: Boolean(item.can_manage)
      }))
    } else {
      const response = await digitalEmployeeApi.listDigitalEmployees({ status: 'active' })
      participantOptions.value = (response.employees || []).map((item) => ({
        value: item.slug,
        label: item.name,
        description: item.digital_employee_profile?.position || '',
        canManage: Boolean(item.can_manage)
      }))
    }
  } catch (error) {
    participantOptions.value = []
    message.error(error.message || '加载会议参与者失败')
  }
}

const loadMeetings = async ({ selectRequested = true } = {}) => {
  loading.value = true
  errorText.value = ''
  try {
    const response = await meetingApi.listMeetings({ kind: props.kind })
    meetings.value = response.meetings || []
    if (selectRequested) {
      const requested = Number(route.query.meeting || 0)
      const requestedMeeting = meetings.value.find((item) => Number(item.id) === requested)
      const initial = requestedMeeting || (requested ? null : meetings.value[0] || null)
      selected.value = initial
      if (initial) await loadDetail(initial.id, { syncRoute: false })
      else if (requested) await loadDetail(requested, { syncRoute: false })
    }
  } catch (error) {
    errorText.value = error.message || '加载会议失败'
    meetings.value = []
    selected.value = null
  } finally {
    loading.value = false
  }
}

const refreshWorkspace = async () => {
  await Promise.all([loadParticipantOptions(), loadMeetings()])
}

const loadDetail = async (id, { syncRoute = true } = {}) => {
  detailLoading.value = true
  try {
    const response = await meetingApi.getMeeting(id)
    if (response.meeting.kind !== props.kind) {
      selected.value = null
      message.error('该会议不属于当前页面')
      return
    }
    selected.value = response.meeting
    const index = meetings.value.findIndex((item) => item.id === response.meeting.id)
    if (index >= 0) meetings.value[index] = response.meeting
    if (syncRoute && Number(route.query.meeting || 0) !== Number(id)) {
      await router.replace({ query: { ...route.query, meeting: String(id) } })
    }
  } catch (error) {
    message.error(error.message || '加载会议详情失败')
  } finally {
    detailLoading.value = false
  }
}

const selectMeeting = async (meeting) => {
  selected.value = meeting
  await loadDetail(meeting.id)
}

const openCreate = () => {
  form.topic = ''
  form.participant_slugs = []
  form.rounds = 2
  createOpen.value = true
}

const createMeeting = async () => {
  if (!form.topic.trim()) {
    message.warning('请填写会议主题')
    return
  }
  if (!form.participant_slugs.length) {
    message.warning('请选择至少一位参与者')
    return
  }
  creating.value = true
  try {
    const response = await meetingApi.createMeeting({
      kind: props.kind,
      topic: form.topic.trim(),
      participant_slugs: form.participant_slugs,
      rounds: form.rounds || 2
    })
    createOpen.value = false
    await loadMeetings({ selectRequested: false })
    selected.value = response.meeting
    await loadDetail(response.meeting.id)
    message.success('会议草稿已创建')
  } catch (error) {
    message.error(error.message || '创建会议失败')
  } finally {
    creating.value = false
  }
}

const runMeeting = async () => {
  if (!selected.value) return
  running.value = true
  try {
    const response = await meetingApi.runMeeting(selected.value.id)
    selected.value = response.meeting
    const index = meetings.value.findIndex((item) => item.id === response.meeting.id)
    if (index >= 0) meetings.value[index] = response.meeting
    message.success('会议纪要已生成')
  } catch (error) {
    message.error(error.message || '生成会议纪要失败')
    await loadDetail(selected.value.id, { syncRoute: false })
  } finally {
    running.value = false
  }
}

const joinMeeting = async () => {
  if (!selected.value || !messageText.value.trim()) return
  messageSending.value = true
  try {
    const response = await meetingApi.addMessage(selected.value.id, {
      content: messageText.value.trim(),
      round: selected.value.rounds || 1
    })
    selected.value = response.meeting
    const index = meetings.value.findIndex((item) => item.id === response.meeting.id)
    if (index >= 0) meetings.value[index] = response.meeting
    messageText.value = ''
  } catch (error) {
    message.error(error.message || '发送会议发言失败')
  } finally {
    messageSending.value = false
  }
}

const openSign = (decision, action) => {
  signForm.decision = decision
  signForm.action = action
  signForm.comment = ''
  signForm.text = action === 'modify' ? decision.effective_text || decision.detail || '' : ''
  signOpen.value = true
}

const submitSign = async () => {
  if (!selected.value || !signForm.decision) return
  if (signForm.action === 'modify' && !signForm.text.trim()) {
    message.warning('修改后签字必须填写修改文本')
    return
  }
  signing.value = true
  try {
    const response = await meetingApi.signDecision(selected.value.id, signForm.decision.id, {
      action: signForm.action,
      comment: signForm.comment || null,
      text: signForm.action === 'modify' ? signForm.text.trim() : null
    })
    selected.value = response.meeting
    const index = meetings.value.findIndex((item) => item.id === response.meeting.id)
    if (index >= 0) meetings.value[index] = response.meeting
    signOpen.value = false
    message.success('签字已保存')
  } catch (error) {
    message.error(error.message || '保存签字失败')
  } finally {
    signing.value = false
  }
}

watch(
  () => route.query.meeting,
  (value) => {
    if (!value) return
    const meeting = meetings.value.find((item) => Number(item.id) === Number(value))
    if (meeting) {
      if (Number(selected.value?.id) !== Number(meeting.id)) {
        selected.value = meeting
        void loadDetail(meeting.id, { syncRoute: false })
      }
      return
    }
    if (Number(selected.value?.id) !== Number(value)) {
      void loadDetail(Number(value), { syncRoute: false })
    }
  }
)

onMounted(async () => {
  await Promise.all([loadParticipantOptions(), loadMeetings()])
})
</script>

<template>
  <section class="meeting-workspace" :class="{ embedded }">
    <header class="workspace-head">
      <div>
        <p class="eyebrow">{{ eyebrow }}</p>
        <h2>{{ title }}</h2>
        <p class="head-desc">
          {{ isTwin ? '让多个分身围绕同一议题给出判断，并把共识落成可签字决议。' : '让数字员工多轮讨论，人可随时入会补充，并把结果交给负责人拍板。' }}
        </p>
      </div>
      <div class="head-actions">
        <a-button :loading="loading" @click="refreshWorkspace">
          <RefreshCw :size="15" />
          刷新
        </a-button>
        <a-button v-if="canCreate" type="primary" @click="openCreate">
          <Plus :size="15" />
          发起会议
        </a-button>
      </div>
    </header>

    <a-alert
      v-if="errorText"
      type="error"
      show-icon
      :message="errorText"
      class="workspace-alert"
    >
      <template #action>
        <a-button size="small" type="text" @click="loadMeetings()">重试</a-button>
      </template>
    </a-alert>

    <div class="workspace-body">
      <aside class="meeting-list">
        <a-spin :spinning="loading">
          <a-empty v-if="!loading && !meetings.length" description="还没有会议" />
          <button
            v-for="meeting in meetings"
            :key="meeting.id"
            type="button"
            class="meeting-card"
            :class="{ active: selected?.id === meeting.id }"
            @click="selectMeeting(meeting)"
          >
            <span class="meeting-card-top">
              <span class="meeting-topic">{{ meeting.topic }}</span>
              <a-tag :color="meetingStatusMeta(meeting.status).color">
                {{ meetingStatusMeta(meeting.status).label }}
              </a-tag>
            </span>
            <span class="meeting-card-meta">
              {{ meeting.participants?.length || 0 }} 位参与者 · 第 {{ meeting.rounds }} 轮
              <template v-if="meeting.pending_decision_count">
                · {{ meeting.pending_decision_count }} 条待签
              </template>
            </span>
            <span class="meeting-card-time">{{ formatTime(meeting.updated_at) }}</span>
          </button>
        </a-spin>
      </aside>

      <main class="meeting-detail">
        <a-spin :spinning="detailLoading">
          <template v-if="selected">
            <header class="detail-head">
              <div>
                <div class="detail-title-row">
                  <h3>{{ selected.topic }}</h3>
                  <a-tag :color="meetingStatusMeta(selected.status).color">
                    {{ meetingStatusMeta(selected.status).label }}
                  </a-tag>
                </div>
                <p class="detail-meta">
                  {{ selected.participants?.map((item) => item.name).join('、') || '未选择参与者' }}
                  · 第 {{ selected.rounds }} 轮 · {{ formatTime(selected.created_at) }}
                </p>
              </div>
              <div class="detail-actions">
                <a-button
                  v-if="meetingCanRun(selected)"
                  type="primary"
                  :loading="running"
                  @click="runMeeting"
                >
                  <Play :size="15" />
                  生成纪要
                </a-button>
              </div>
            </header>

            <a-alert
              v-if="selected.generation_error"
              type="warning"
              show-icon
              :message="`上次生成失败：${selected.generation_error}`"
              class="detail-alert"
            />

            <a-empty
              v-if="selected.status === 'draft' && !selected.messages?.length"
              description="会议还是草稿，点击“生成纪要”让参与者开始讨论"
            />

            <template v-else>
              <section class="summary-block">
                <h4>会议结论</h4>
                <p>{{ selected.summary || '暂无结论' }}</p>
                <div v-if="selected.consensus?.length" class="summary-list">
                  <strong>共识</strong>
                  <ul>
                    <li v-for="item in selected.consensus" :key="item">{{ item }}</li>
                  </ul>
                </div>
                <div v-if="selected.divergences?.length" class="summary-list">
                  <strong>分歧</strong>
                  <div v-for="item in selected.divergences" :key="item.topic" class="divergence">
                    <span>{{ item.topic }}</span>
                    <p
                      v-for="position in item.positions || []"
                      :key="`${item.topic}-${position.participant}`"
                    >
                      {{ participantName(position.participant) }}：{{ position.view }}
                    </p>
                  </div>
                </div>
              </section>

              <section class="timeline-block">
                <h4>会议发言</h4>
                <a-empty v-if="!selected.messages?.length" description="还没有会议发言" />
                <article
                  v-for="item in selected.messages"
                  :key="item.id"
                  class="message-card"
                >
                  <div class="message-head">
                    <span class="speaker">
                      {{ meetingSpeakerName(item) }}
                      <a-tag>{{ speakerTypeLabel(item.speaker_type) }}</a-tag>
                    </span>
                    <span class="round">第 {{ item.round }} 轮 · {{ formatTime(item.created_at) }}</span>
                  </div>
                  <p class="message-content">{{ item.content }}</p>
                  <div v-if="item.citations?.length" class="citation-list">
                    <span>引用：</span>
                    <a-tag v-for="citation in item.citations" :key="citation.entry_id">
                      {{ citation.title || `经验 #${citation.entry_id}` }}
                    </a-tag>
                  </div>
                </article>
              </section>

              <section class="decision-block">
                <div class="block-head">
                  <h4>会议决议</h4>
                  <span v-if="selectedPendingCount" class="pending-hint">
                    {{ selectedPendingCount }} 条等待负责人签字
                  </span>
                </div>
                <a-empty v-if="!selected.decisions?.length" description="本次会议没有形成决议" />
                <article
                  v-for="decision in selected.decisions"
                  :key="decision.id"
                  class="decision-card"
                >
                  <div class="decision-head">
                    <div>
                      <h5>{{ decision.code }} · {{ decision.title }}</h5>
                      <p class="decision-meta">
                        提议人 {{ participantName(decision.proposer_slug) }} ·
                        <a-tag :color="decisionStatusMeta(decision.status).color">
                          {{ decisionStatusMeta(decision.status).label }}
                        </a-tag>
                      </p>
                    </div>
                    <div v-if="decision.can_sign" class="decision-actions">
                      <a-button size="small" type="primary" @click="openSign(decision, 'approve')">
                        <Check :size="14" />
                        同意
                      </a-button>
                      <a-button size="small" @click="openSign(decision, 'modify')">修改后同意</a-button>
                      <a-button size="small" danger @click="openSign(decision, 'reject')">
                        <X :size="14" />
                        拒绝
                      </a-button>
                    </div>
                  </div>
                  <p class="decision-detail">{{ decision.detail }}</p>
                  <p v-if="decision.impact" class="decision-impact">影响：{{ decision.impact }}</p>
                  <p v-if="decision.effective_text" class="decision-effective">
                    生效文本：{{ decision.effective_text }}
                  </p>
                  <div class="signer-list">
                    <span
                      v-for="signer in decision.signers"
                      :key="signer.uid"
                      class="signer"
                    >
                      {{ signer.name }}
                      <a-tag :color="signerStatusMeta(signer.status).color">
                        {{ signerStatusMeta(signer.status).label }}
                      </a-tag>
                      <small v-if="signer.comment">“{{ signer.comment }}”</small>
                    </span>
                  </div>
                </article>
              </section>
            </template>

            <section v-if="meetingCanJoin(selected)" class="join-block">
              <h4>人入会</h4>
              <div class="join-row">
                <a-textarea
                  v-model:value="messageText"
                  :rows="2"
                  :maxlength="3000"
                  placeholder="以人的身份补充一条会议意见"
                />
                <a-button
                  type="primary"
                  :loading="messageSending"
                  :disabled="!messageText.trim()"
                  @click="joinMeeting"
                >
                  <Send :size="15" />
                  发言
                </a-button>
              </div>
            </section>
          </template>
          <a-empty v-else description="请选择一场会议" />
        </a-spin>
      </main>
    </div>

    <a-modal
      v-model:open="createOpen"
      title="发起会议"
      :confirm-loading="creating"
      ok-text="创建草稿"
      @ok="createMeeting"
    >
      <a-form layout="vertical">
        <a-form-item label="会议主题" required>
          <a-input v-model:value="form.topic" :maxlength="300" placeholder="例如：大客户报价流程怎么统一" />
        </a-form-item>
        <a-form-item :label="isTwin ? '参与分身' : '参与数字员工'" required>
          <a-select
            v-model:value="form.participant_slugs"
            mode="multiple"
            :options="participantOptions"
            :max-tag-count="4"
            placeholder="选择参与者"
          />
        </a-form-item>
        <a-form-item label="讨论轮次">
          <a-input-number v-model:value="form.rounds" :min="1" :max="4" style="width: 100%" />
        </a-form-item>
        <p class="modal-hint">会议先保存为草稿；生成纪要和决议需要调用系统默认模型。</p>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="signOpen"
      :title="signForm.action === 'reject' ? '拒绝决议' : '决议签字'"
      :confirm-loading="signing"
      ok-text="提交签字"
      @ok="submitSign"
    >
      <p class="sign-decision-title">{{ signForm.decision?.title }}</p>
      <a-form layout="vertical">
        <a-form-item v-if="signForm.action === 'modify'" label="修改后的决议文本" required>
          <a-textarea v-model:value="signForm.text" :rows="4" :maxlength="4000" />
        </a-form-item>
        <a-form-item label="签字意见">
          <a-textarea
            v-model:value="signForm.comment"
            :rows="2"
            :maxlength="1000"
            placeholder="可选，写下你的判断或保留意见"
          />
        </a-form-item>
      </a-form>
    </a-modal>
  </section>
</template>

<style scoped>
.meeting-workspace {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 520px;
}

.meeting-workspace.embedded {
  padding-top: 4px;
}

.workspace-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.eyebrow {
  margin: 0 0 4px;
  color: var(--gray-500);
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.workspace-head h2 {
  margin: 0;
  color: var(--gray-900);
  font-size: 22px;
}

.head-desc {
  max-width: 720px;
  margin: 6px 0 0;
  color: var(--gray-600);
  line-height: 1.6;
}

.head-actions,
.detail-actions,
.decision-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.workspace-alert,
.detail-alert {
  margin: 0;
}

.workspace-body {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  min-height: 560px;
}

.meeting-list {
  padding: 10px;
  border: 1px solid var(--gray-200);
  border-radius: 14px;
  background: var(--gray-25);
}

.meeting-card {
  display: flex;
  width: 100%;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
  padding: 12px;
  border: 1px solid transparent;
  border-radius: 12px;
  background: var(--gray-0);
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.meeting-card:hover,
.meeting-card.active {
  border-color: color-mix(in srgb, var(--primary-color) 40%, var(--gray-200));
}

.meeting-card.active {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary-color) 10%, transparent);
}

.meeting-card-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.meeting-topic {
  color: var(--gray-900);
  font-weight: 600;
  line-height: 1.45;
}

.meeting-card-meta,
.meeting-card-time {
  color: var(--gray-500);
  font-size: 12px;
}

.meeting-detail {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--gray-200);
  border-radius: 14px;
  background: var(--gray-0);
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--gray-150);
}

.detail-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.detail-title-row h3 {
  margin: 0;
  color: var(--gray-900);
  font-size: 18px;
}

.detail-meta,
.decision-meta,
.meeting-card-meta {
  margin: 6px 0 0;
  color: var(--gray-500);
  font-size: 13px;
}

.summary-block,
.timeline-block,
.decision-block,
.join-block {
  margin-top: 18px;
}

h4 {
  margin: 0 0 10px;
  color: var(--gray-900);
}

.summary-block p {
  margin: 0;
  color: var(--gray-800);
  line-height: 1.7;
}

.summary-list {
  margin-top: 10px;
  color: var(--gray-700);
}

.summary-list ul {
  margin: 6px 0 0;
  padding-left: 20px;
}

.divergence {
  margin-top: 8px;
  padding: 10px;
  border-radius: 8px;
  background: var(--gray-50);
}

.divergence span {
  font-weight: 600;
}

.divergence p {
  margin: 4px 0 0;
  color: var(--gray-600);
}

.message-card,
.decision-card {
  padding: 14px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
}

.message-card + .message-card,
.decision-card + .decision-card {
  margin-top: 10px;
}

.message-head,
.decision-head,
.block-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.speaker {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-900);
  font-weight: 600;
}

.round,
.pending-hint {
  color: var(--gray-500);
  font-size: 12px;
}

.message-content,
.decision-detail,
.decision-impact,
.decision-effective {
  margin: 10px 0 0;
  color: var(--gray-700);
  line-height: 1.7;
  white-space: pre-wrap;
}

.decision-impact,
.decision-effective {
  font-size: 13px;
}

.citation-list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  color: var(--gray-500);
  font-size: 12px;
}

.decision-card + .decision-card {
  margin-top: 12px;
}

.decision-card h5 {
  margin: 0;
  color: var(--gray-900);
  font-size: 15px;
}

.signer-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--gray-200);
}

.signer {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-700);
  font-size: 13px;
}

.signer small {
  color: var(--gray-500);
}

.join-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.join-row :deep(.ant-input) {
  flex: 1;
}

.modal-hint,
.sign-decision-title {
  margin: 0;
  color: var(--gray-500);
  line-height: 1.6;
}

@media (max-width: 960px) {
  .workspace-body {
    grid-template-columns: 1fr;
  }

  .meeting-list {
    max-height: 220px;
    overflow-y: auto;
  }

  .workspace-head,
  .detail-head,
  .join-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
