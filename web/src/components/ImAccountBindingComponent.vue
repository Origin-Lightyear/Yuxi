<template>
  <div class="im-account-binding">
    <div class="header-section">
      <div class="header-content">
        <div class="section-title">IM 账号</div>
        <p class="section-description">绑定飞书 / 钉钉账号，用于需要身份确认的 IM 能力。</p>
      </div>
    </div>

    <a-spin :spinning="state.loading">
      <a-alert v-if="state.error" type="error" :message="state.error" show-icon class="binding-error" />

      <div class="platform-list">
        <div v-for="platform in platforms" :key="platform.key" class="platform-item">
          <div class="platform-info">
            <span class="platform-name">{{ platform.label }}</span>
            <span v-if="bindingFor(platform.key)" class="platform-bound">
              已绑定：{{ bindingFor(platform.key).platform_user_id }}
            </span>
            <span v-else class="platform-unbound">未绑定</span>
          </div>
          <div class="platform-action">
            <a-button v-if="bindingFor(platform.key)" danger @click="confirmUnbind(platform.key)">
              解绑
            </a-button>
            <a-button v-else type="primary" @click="startBind(platform.key)">
              绑定{{ platform.label }}
            </a-button>
          </div>
        </div>
      </div>
    </a-spin>

    <a-modal
      v-model:open="state.bindModalVisible"
      :title="`扫码绑定${currentPlatformLabel}`"
      :footer="null"
      :mask-closable="false"
      width="320px"
      class="bind-modal"
      @cancel="closeBindModal"
    >
      <div class="bind-modal-body">
        <img v-if="state.qrDataUrl" :src="state.qrDataUrl" class="qr-image" alt="绑定二维码" />
        <a-spin v-else :spinning="true" />
        <p class="bind-tip">使用{{ currentPlatformLabel }}扫码完成绑定，绑定成功后自动关闭</p>
      </div>
    </a-modal>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive } from 'vue'
import { notification, Modal } from 'ant-design-vue'
import { toDataURL } from 'qrcode'
import { imBindingApi } from '@/apis/im_binding_api'

const POLL_INTERVAL_MS = 3000

const platforms = [
  { key: 'feishu', label: '飞书' },
  { key: 'dingtalk', label: '钉钉' }
]

const state = reactive({
  loading: false,
  error: null,
  bindings: [],
  bindModalVisible: false,
  bindingPlatform: null,
  qrDataUrl: ''
})

const currentPlatformLabel = computed(() => {
  const platform = platforms.find((item) => item.key === state.bindingPlatform)
  return platform ? platform.label : ''
})

let pollTimer = null

const bindingFor = (platform) => state.bindings.find((item) => item.platform === platform)

const fetchBindings = async () => {
  try {
    state.loading = true
    state.error = null
    const result = await imBindingApi.list()
    state.bindings = result.data || []
  } catch (error) {
    console.error('获取 IM 绑定失败:', error)
    state.error = '获取 IM 绑定失败'
  } finally {
    state.loading = false
  }
}

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const startPolling = (platform) => {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const result = await imBindingApi.list()
      const bound = (result.data || []).some((item) => item.platform === platform)
      if (bound) {
        stopPolling()
        state.bindModalVisible = false
        state.bindingPlatform = null
        state.qrDataUrl = ''
        notification.success({ message: '绑定成功' })
        await fetchBindings()
      }
    } catch {
      // 轮询失败保持现状，等待下一轮重试
    }
  }, POLL_INTERVAL_MS)
}

const startBind = async (platform) => {
  try {
    const result = await imBindingApi.createSession(platform)
    state.bindingPlatform = platform
    state.qrDataUrl = await toDataURL(result.data.bind_url)
    state.bindModalVisible = true
    startPolling(platform)
  } catch (error) {
    notification.error({ message: '发起绑定失败', description: error.message || '请稍后重试' })
  }
}

const closeBindModal = () => {
  stopPolling()
  state.bindModalVisible = false
  state.bindingPlatform = null
  state.qrDataUrl = ''
}

const confirmUnbind = (platform) => {
  Modal.confirm({
    title: '确认解绑',
    content: '解绑后将无法使用该平台的绑定能力，是否继续？',
    okText: '解绑',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        await imBindingApi.unbind(platform)
        notification.success({ message: '解绑成功' })
        await fetchBindings()
      } catch (error) {
        notification.error({ message: '解绑失败', description: error.message || '请稍后重试' })
      }
    }
  })
}

onMounted(fetchBindings)
onUnmounted(stopPolling)
</script>

<style lang="less" scoped>
.im-account-binding {
  .header-section {
    .header-content {
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
  }

  .binding-error {
    margin-bottom: 16px;
  }

  .platform-list {
    margin-top: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .platform-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 16px;
    border: 1px solid var(--gray-150);
    border-radius: 10px;
  }

  .platform-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }

  .platform-name {
    font-weight: 500;
    color: var(--gray-900);
  }

  .platform-bound {
    font-size: 13px;
    color: var(--gray-600);
    word-break: break-all;
  }

  .platform-unbound {
    font-size: 13px;
    color: var(--gray-500);
  }
}

.bind-modal {
  .bind-modal-body {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;

    .qr-image {
      width: 200px;
      height: 200px;
      border: 1px solid var(--gray-150);
      border-radius: 8px;
    }

    .bind-tip {
      margin: 0;
      font-size: 13px;
      color: var(--gray-600);
      text-align: center;
    }
  }
}
</style>
