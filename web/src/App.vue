<script setup>
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import { useAgentStore } from '@/stores/agent'
import { useUserStore } from '@/stores/user'
import { useThemeStore } from '@/stores/theme'
import { onMounted } from 'vue'
import ConnectionStatusBanner from '@/components/ConnectionStatusBanner.vue'
import { connectionMonitor } from '@/utils/connectionMonitor'
import { scheduledRunPoller } from '@/utils/scheduledRunPoller'

const agentStore = useAgentStore()
const userStore = useUserStore()
const themeStore = useThemeStore()

onMounted(async () => {
  // 桌面壳专属接线：非 Electron 环境 fail-closed，浏览器零变化
  if (globalThis.window?.yuxiDesktop) {
    scheduledRunPoller.init()
    connectionMonitor.start()
  }
  if (userStore.isLoggedIn) {
    await agentStore.initialize()
  }
})
</script>
<template>
  <a-config-provider :theme="themeStore.currentTheme" :locale="zhCN">
    <ConnectionStatusBanner />
    <router-view />
  </a-config-provider>
</template>
