<template>
  <div class="im-bot-management">
    <!-- 头部区域 -->
    <div class="header-section">
      <div class="header-content">
        <div class="section-title">IM 机器人</div>
        <p class="section-description">
          管理飞书 / 钉钉机器人，将其接入指定智能体与归属用户。
        </p>
      </div>
      <div class="header-actions">
        <a-button
          @click="fetchBots"
          :loading="state.loading"
          title="刷新"
          class="refresh-btn lucide-icon-btn"
        >
          <template #icon>
            <RefreshCw :size="16" :class="{ spin: state.loading }" />
          </template>
        </a-button>
        <a-button type="primary" @click="openCreateModal" class="add-btn lucide-icon-btn">
          <template #icon><Plus :size="16" /></template>
          新增机器人
        </a-button>
      </div>
    </div>

    <!-- 主内容区域 -->
    <div class="content-section">
      <a-spin :spinning="state.loading">
        <div v-if="state.error" class="error-message">
          <a-alert type="error" :message="state.error" show-icon />
        </div>

        <template v-if="state.bots.length > 0">
          <a-table
            :dataSource="state.bots"
            :columns="columns"
            :rowKey="(record) => record.id"
            :pagination="false"
            class="im-bot-table"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'name'">
                <span class="name-text">{{ record.name }}</span>
              </template>
              <template v-if="column.key === 'platform'">
                <a-tag :color="record.platform === 'dingtalk' ? 'blue' : 'cyan'">
                  {{ platformLabel(record.platform) }}
                </a-tag>
              </template>
              <template v-if="column.key === 'agentSlug'">
                <span class="agent-text">{{ record.agent_slug || '-' }}</span>
              </template>
              <template v-if="column.key === 'enabled'">
                <a-switch
                  :checked="record.enabled"
                  @change="(checked) => toggleEnabled(record, checked)"
                />
              </template>
              <template v-if="column.key === 'action'">
                <a-space>
                  <a-tooltip title="编辑机器人">
                    <a-button
                      type="text"
                      size="small"
                      @click="openEditModal(record)"
                      class="action-btn lucide-icon-btn"
                    >
                      <SquarePen :size="14" />
                    </a-button>
                  </a-tooltip>
                  <a-tooltip title="删除机器人">
                    <a-button
                      type="text"
                      size="small"
                      danger
                      @click="confirmDelete(record)"
                      class="action-btn lucide-icon-btn"
                    >
                      <Trash2 :size="14" />
                    </a-button>
                  </a-tooltip>
                </a-space>
              </template>
            </template>
          </a-table>
        </template>

        <div v-else class="empty-state">
          <a-empty description="暂无 IM 机器人" />
        </div>
      </a-spin>
    </div>

    <!-- 新增 / 编辑表单模态框 -->
    <a-modal
      v-model:open="state.modalVisible"
      :title="state.editMode ? '编辑机器人' : '新增机器人'"
      @ok="submit"
      :confirmLoading="state.saving"
      @cancel="state.modalVisible = false"
      :maskClosable="false"
      width="560px"
      class="im-bot-modal"
    >
      <a-form layout="vertical" class="im-bot-form" autocomplete="off">
        <a-form-item label="平台" required class="form-item">
          <a-select
            v-model:value="state.form.platform"
            :options="platformOptions"
            :disabled="state.editMode"
            size="large"
          />
        </a-form-item>

        <a-form-item label="名称" required class="form-item">
          <a-input
            v-model:value="state.form.name"
            placeholder="请输入机器人名称"
            size="large"
            :maxlength="100"
          />
        </a-form-item>

        <a-form-item label="归属用户" required class="form-item">
          <a-select
            v-model:value="state.form.ownerUid"
            :options="ownerOptions"
            show-search
            :filterOption="filterOption"
            placeholder="请选择归属用户"
            size="large"
          />
        </a-form-item>

        <a-form-item label="目标 Agent" required class="form-item">
          <a-select
            v-model:value="state.form.agentSlug"
            :options="agentOptions"
            show-search
            :filterOption="filterOption"
            placeholder="请选择目标 Agent"
            size="large"
          />
        </a-form-item>

        <a-divider orientation="left">凭证</a-divider>

        <template v-if="state.form.platform === 'feishu'">
          <a-form-item label="App ID" :required="!state.editMode" class="form-item">
            <a-input
              v-model:value="state.form.credentials.app_id"
              placeholder="请输入飞书 App ID"
              size="large"
            />
          </a-form-item>
          <a-form-item label="App Secret" :required="!state.editMode" class="form-item">
            <a-input-password
              v-model:value="state.form.credentials.app_secret"
              placeholder="请输入飞书 App Secret"
              size="large"
            />
          </a-form-item>
          <a-form-item label="Domain" class="form-item">
            <a-input
              v-model:value="state.form.credentials.domain"
              placeholder="feishu（默认）"
              size="large"
            />
          </a-form-item>
        </template>

        <template v-else>
          <a-form-item label="Client ID" :required="!state.editMode" class="form-item">
            <a-input
              v-model:value="state.form.credentials.client_id"
              placeholder="请输入钉钉 Client ID"
              size="large"
            />
          </a-form-item>
          <a-form-item label="Client Secret" :required="!state.editMode" class="form-item">
            <a-input-password
              v-model:value="state.form.credentials.client_secret"
              placeholder="请输入钉钉 Client Secret"
              size="large"
            />
          </a-form-item>
        </template>

        <div v-if="state.editMode" class="credential-hint">留空表示不修改凭证</div>

        <a-divider orientation="left">策略</a-divider>

        <a-form-item label="白名单" class="form-item">
          <a-textarea
            v-model:value="state.form.allowedUsersText"
            placeholder="留空表示允许所有人"
            :rows="3"
          />
          <div class="help-text">留空表示允许所有人，多个用户可用逗号或空白分隔</div>
        </a-form-item>

        <a-form-item label="群聊需要 @机器人" class="form-item">
          <a-switch v-model:checked="state.form.requireMention" />
        </a-form-item>

        <a-form-item label="群聊内按用户独立会话" class="form-item">
          <a-switch v-model:checked="state.form.groupSessionsPerUser" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { h, onMounted, reactive, ref } from 'vue'
import { notification, Modal } from 'ant-design-vue'
import { Plus, RefreshCw, SquarePen, Trash2 } from '@lucide/vue'
import { agentApi, imBotApi } from '@/apis'
import { authApi } from '@/apis/auth_api'
import { buildImBotPayload, formFromImBot } from '@/utils/imBotForm'

const platformOptions = [
  { label: '飞书', value: 'feishu' },
  { label: '钉钉', value: 'dingtalk' }
]

const columns = [
  { title: '名称', dataIndex: 'name', key: 'name', width: 180 },
  { title: '平台', dataIndex: 'platform', key: 'platform', width: 90, align: 'center' },
  { title: '目标 Agent', dataIndex: 'agent_slug', key: 'agentSlug', ellipsis: true },
  { title: '状态', dataIndex: 'enabled', key: 'enabled', width: 90, align: 'center' },
  { title: '操作', key: 'action', width: 120, align: 'center' }
]

const state = reactive({
  loading: false,
  bots: [],
  error: null,
  modalVisible: false,
  editMode: false,
  editId: null,
  saving: false,
  form: formFromImBot({})
})

const ownerOptions = ref([])
const agentOptions = ref([])

/** 平台文案映射 */
const platformLabel = (platform) => (platform === 'dingtalk' ? '钉钉' : '飞书')

/** 下拉框按 label 做大小写不敏感的子串过滤 */
const filterOption = (input, option) =>
  String(option.label || '')
    .toLowerCase()
    .includes(String(input).toLowerCase())

/** 获取 IM 机器人列表 */
const fetchBots = async () => {
  try {
    state.loading = true
    state.error = null
    const result = await imBotApi.list()
    state.bots = result.data || []
  } catch (error) {
    console.error('获取 IM 机器人失败:', error)
    state.error = '获取 IM 机器人失败'
  } finally {
    state.loading = false
  }
}

/** 加载归属用户与目标 Agent 下拉选项 */
const loadOptions = async () => {
  try {
    const [users, agentsResult] = await Promise.all([
      authApi.getUserAccessOptions(),
      agentApi.getAgents({ includeSubagents: false })
    ])
    ownerOptions.value = (users || []).map((user) => ({
      label: user.department_name ? `${user.username}（${user.department_name}）` : user.username,
      value: user.uid
    }))
    agentOptions.value = (agentsResult.agents || []).map((agent) => ({
      label: agent.name || agent.slug,
      value: agent.slug || agent.agent_id || agent.id
    }))
  } catch (error) {
    console.error('加载归属用户 / 目标 Agent 失败:', error)
  }
}

/** 打开新增模态框，清空表单并启用凭证输入 */
const openCreateModal = () => {
  state.editMode = false
  state.editId = null
  state.form = { ...formFromImBot({}), credentials: {} }
  state.modalVisible = true
}

/** 打开编辑模态框，回填非凭证字段 */
const openEditModal = (bot) => {
  state.editMode = true
  state.editId = bot.id
  state.form = { ...formFromImBot(bot), credentials: {} }
  state.modalVisible = true
}

/** 根据平台从表单凭证输入中收集载荷，全部为空时返回 null 表示不修改 */
const collectCredentials = (form) => {
  const credentials = form.credentials
  if (form.platform === 'feishu') {
    const { app_id, app_secret, domain } = credentials
    if (!app_id && !app_secret && !domain) return null
    return { app_id, app_secret, domain }
  }
  const { client_id, client_secret } = credentials
  if (!client_id && !client_secret) return null
  return { client_id, client_secret }
}

/** 校验表单，返回错误提示文案，通过时返回 null */
const validateForm = () => {
  if (!state.form.name.trim()) return '请输入名称'
  if (!state.form.ownerUid) return '请选择归属用户'
  if (!state.form.agentSlug) return '请选择目标 Agent'
  if (!state.editMode) {
    const requiredFields =
      state.form.platform === 'feishu' ? ['app_id', 'app_secret'] : ['client_id', 'client_secret']
    if (requiredFields.some((field) => !state.form.credentials[field])) return '请填写必填凭证'
  }
  return null
}

/** 提交新增 / 编辑 */
const submit = async () => {
  const errorMessage = validateForm()
  if (errorMessage) {
    notification.error({ message: errorMessage })
    return
  }

  state.saving = true
  try {
    const formForSubmit = { ...state.form, credentials: collectCredentials(state.form) }
    const payload = buildImBotPayload(formForSubmit)
    if (state.editMode) await imBotApi.update(state.editId, payload)
    else await imBotApi.create(payload)
    notification.success({ message: '保存成功' })
    state.modalVisible = false
    await fetchBots()
  } catch (error) {
    notification.error({ message: '保存失败', description: error.message || '请稍后重试' })
  } finally {
    state.saving = false
  }
}

/** 切换启用状态，失败时保持原状态 */
const toggleEnabled = async (record, checked) => {
  try {
    await imBotApi.setEnabled(record.id, checked)
    record.enabled = checked
  } catch (error) {
    notification.error({ message: '切换状态失败', description: error.message || '请稍后重试' })
  }
}

/** 删除机器人 */
const confirmDelete = (record) => {
  Modal.confirm({
    title: '确认删除机器人',
    content: h('span', [
      '确定要删除机器人 ',
      h('strong', record.name),
      ' 吗？该操作不可撤销，关联的 API Key 也会一并回收。'
    ]),
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        await imBotApi.remove(record.id)
        notification.success({ message: '删除成功' })
        await fetchBots()
      } catch (error) {
        notification.error({ message: '删除失败', description: error.message || '请稍后重试' })
      }
    }
  })
}

onMounted(() => {
  fetchBots()
  loadOptions()
})
</script>

<style lang="less" scoped>
.im-bot-management {
  .header-section {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    margin-bottom: 16px;

    .header-content {
      flex: 1;
      min-width: 0;

      .section-title {
        font-size: 16px;
        font-weight: 500;
        color: var(--gray-900);
        line-height: 1.4;
        margin: 12px 0 12px;
      }

      .section-description {
        font-size: 14px;
        color: var(--gray-600);
        line-height: 1.4;
        margin: 0;
      }
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 8px;

      .refresh-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 32px;
        height: 32px;
        border-radius: 6px;
        transition: all 0.2s ease;

        &:hover {
          background: var(--gray-25);
        }

        .spin {
          animation: spin 1s linear infinite;
        }
      }
    }
  }

  .content-section {
    overflow: hidden;

    .error-message {
      padding: 16px 24px;
    }

    .empty-state {
      padding: 60px 20px;
      text-align: center;
    }

    .im-bot-table {
      :deep(.ant-table-thead > tr > th) {
        background: var(--gray-50);
        font-weight: 500;
        padding: 8px 12px;
      }

      :deep(.ant-table-tbody > tr > td) {
        padding: 8px 12px;
      }

      .name-text {
        font-weight: 500;
        color: var(--gray-900);
      }

      .agent-text {
        color: var(--gray-600);
      }

      .action-btn {
        padding: 4px 8px;
        border-radius: 6px;
        transition: all 0.2s ease;

        &:hover {
          background: var(--gray-25);
        }
      }
    }
  }
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.im-bot-modal {
  :deep(.ant-modal-header) {
    padding: 20px 24px;
    border-bottom: 1px solid var(--gray-150);

    .ant-modal-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--gray-900);
    }
  }

  :deep(.ant-modal-body) {
    padding: 24px;
  }

  .im-bot-form {
    .form-item {
      margin-bottom: 20px;

      :deep(.ant-form-item-label) {
        padding-bottom: 4px;

        label {
          font-weight: 500;
          color: var(--gray-900);
        }
      }
    }
  }

  .credential-hint,
  .help-text {
    color: var(--gray-600);
    font-size: 12px;
    line-height: 1.3;
  }

  .credential-hint {
    margin-top: -12px;
    margin-bottom: 12px;
  }

  .help-text {
    margin-top: 4px;
  }
}
</style>
