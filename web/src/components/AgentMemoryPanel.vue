<template>
  <a-modal
    :open="open && enableMemory"
    class="agent-memory-modal"
    :width="560"
    :footer="null"
    :closable="false"
    @cancel="emit('close')"
  >
    <template #title>
      <div class="agent-memory-titlebar">
        <span class="agent-memory-title">记忆</span>
        <button type="button" class="agent-memory-close" aria-label="关闭" @click="emit('close')">
          <X :size="16" />
        </button>
      </div>
    </template>
    <div class="agent-memory-body">
      <a-spin :spinning="loading">
        <div v-if="!loading && !memories.length" class="agent-memory-empty" role="status">
          暂无记忆
        </div>
        <ul v-else class="agent-memory-list">
          <li v-for="item in memories" :key="item.id" class="agent-memory-item">
            <div class="agent-memory-item-main">
              <p class="agent-memory-item-content">{{ item.content }}</p>
              <span class="agent-memory-item-time">{{ formatTime(item.created_at) }}</span>
            </div>
            <button
              type="button"
              class="agent-memory-item-delete"
              :aria-label="`删除记忆：${item.content}`"
              :disabled="deletingId !== null"
              @click="confirmDelete(item)"
            >
              <Trash2 :size="14" />
            </button>
          </li>
        </ul>
      </a-spin>
    </div>
  </a-modal>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { message, Modal } from 'ant-design-vue'
import dayjs from 'dayjs'
import { Trash2, X } from '@lucide/vue'
import { agentMemoryApi } from '@/apis/agent_memory_api'
import { userConfigApi } from '@/apis/user_config_api'

const props = defineProps({
  agentSlug: { type: String, required: true },
  open: { type: Boolean, default: false }
})

const emit = defineEmits(['close'])

const enableMemory = ref(false)
const loading = ref(false)
const deletingId = ref(null)
const memories = ref([])

const formatTime = (value) => (value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '')

const loadMemories = async () => {
  if (!props.agentSlug) return
  loading.value = true
  try {
    const res = await agentMemoryApi.list(props.agentSlug)
    memories.value = res.items || []
  } catch (error) {
    message.error(error.message || '加载记忆失败')
  } finally {
    loading.value = false
  }
}

const confirmDelete = (item) => {
  Modal.confirm({
    title: '删除这条记忆？',
    icon: null,
    centered: true,
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    content: item.content,
    onOk: async () => {
      try {
        await agentMemoryApi.remove(props.agentSlug, item.id)
        memories.value = memories.value.filter((memory) => memory.id !== item.id)
      } catch (error) {
        message.error(error.message || '删除记忆失败')
      }
    }
  })
}

watch(
  () => props.open,
  (open) => {
    if (open && enableMemory.value) loadMemories()
  }
)

onMounted(async () => {
  try {
    const res = await userConfigApi.get()
    enableMemory.value = Boolean(res.enable_memory)
    if (props.open && enableMemory.value) loadMemories()
  } catch (error) {
    message.error(error.message || '加载用户配置失败')
  }
})

defineExpose({ refresh: loadMemories })
</script>

<style lang="less" scoped>
.agent-memory-titlebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;

  .agent-memory-title {
    font-size: 15px;
    font-weight: 600;
    color: var(--gray-1000);
  }

  .agent-memory-close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 4px;
    border: none;
    background: transparent;
    color: var(--gray-600);
    cursor: pointer;
    border-radius: 6px;

    &:hover {
      background: var(--gray-100);
      color: var(--gray-1000);
    }
  }
}

.agent-memory-body {
  min-height: 120px;
  max-height: 420px;
  overflow-y: auto;

  .agent-memory-empty {
    padding: 32px 8px;
    color: var(--gray-500);
    text-align: center;
    font-size: 13px;
  }
}

.agent-memory-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.agent-memory-item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--gray-150);

  &:last-child {
    border-bottom: none;
  }
}

.agent-memory-item-main {
  flex: 1;
  min-width: 0;
}

.agent-memory-item-content {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--gray-1000);
  white-space: pre-wrap;
  word-break: break-word;
}

.agent-memory-item-time {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--gray-500);
}

.agent-memory-item-delete {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 4px;
  border: none;
  background: transparent;
  color: var(--gray-500);
  cursor: pointer;
  border-radius: 6px;

  &:hover:not(:disabled) {
    background: var(--gray-100);
    color: #d9363e;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}
</style>
