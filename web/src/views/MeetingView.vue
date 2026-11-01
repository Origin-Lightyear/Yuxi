<script setup>
import PageHeader from '@/components/shared/PageHeader.vue'
import MeetingWorkspace from '@/components/meeting/MeetingWorkspace.vue'
</script>

<template>
  <div class="meetings-page">
    <PageHeader title="AI 会议" :show-border="true">
      <template #info>
        <span class="head-info">数字员工多轮讨论、人可入会、负责人拍板</span>
      </template>
    </PageHeader>
    <main class="meetings-body">
      <MeetingWorkspace kind="digital_employee" />
    </main>
  </div>
</template>

<style scoped>
.meetings-page {
  min-height: 100vh;
  background: var(--gray-25);
}

.head-info {
  color: var(--gray-500);
  font-size: 13px;
}

.meetings-body {
  padding: 16px var(--page-padding) 40px;
}
</style>
