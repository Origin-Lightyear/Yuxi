<script setup>
import { onUnmounted, ref } from 'vue'
import { connectionMonitor } from '@/utils/connectionMonitor'

// 桌面门控在 setup 处闭合：非 Electron 环境不订阅、渲染 null，浏览器零变化
const isDesktop = !!globalThis.window?.yuxiDesktop
const status = ref(isDesktop ? connectionMonitor.state() : 'online')

let unsubscribe = null
if (isDesktop) {
  unsubscribe = connectionMonitor.subscribe((next) => {
    status.value = next
  })
}
onUnmounted(() => unsubscribe?.())
</script>

<template>
  <div
    v-if="isDesktop && status === 'offline'"
    role="alert"
    class="connection-status-banner"
  >
    连接已断开，正在重试…
  </div>
</template>

<style scoped>
.connection-status-banner {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1100;
  padding: 8px 16px;
  text-align: center;
  font-size: 14px;
  color: var(--color-error-900);
  background: var(--color-error-100);
}
</style>
