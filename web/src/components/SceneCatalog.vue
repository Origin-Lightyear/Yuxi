<template>
  <div class="scene-catalog">
    <button
      v-if="dailyTip"
      type="button"
      class="scene-daily-tip"
      @click="emit('select', dailyTip.agent_id)"
    >
      <Lightbulb :size="16" aria-hidden="true" />
      <span class="scene-daily-tip-text">{{ dailyTip.tip }}</span>
      <span class="scene-daily-tip-name">{{ dailyTip.name }}</span>
    </button>

    <div v-for="group in scenes" :key="group.scene" class="scene-group">
      <p class="scene-group-label">{{ group.label }}</p>
      <div class="scene-template-list">
        <button
          v-for="template in group.templates"
          :key="template.agent_id"
          type="button"
          class="scene-template-card"
          @click="emit('select', template.agent_id)"
        >
          <component :is="sceneIcon(template.icon)" :size="18" class="scene-template-icon" />
          <span class="scene-template-info">
            <span class="scene-template-name">{{ template.name }}</span>
            <span class="scene-template-desc">{{ template.description }}</span>
          </span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { BarChart3, Bot, FileText, Lightbulb, MessageCircle, PenLine, Telescope } from '@lucide/vue'

defineProps({
  scenes: { type: Array, default: () => [] },
  dailyTip: { type: Object, default: null }
})
const emit = defineEmits(['select'])

const SCENE_TEMPLATE_ICONS = {
  telescope: Telescope,
  'bar-chart-3': BarChart3,
  'pen-line': PenLine,
  'message-circle': MessageCircle,
  'file-text': FileText
}

/** 预置 icon 是 Lucide 名称；未知名称回退到通用智能体图标。 */
const sceneIcon = (name) => SCENE_TEMPLATE_ICONS[name] || Bot
</script>

<style lang="less" scoped>
.scene-catalog {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 12px;
}

.scene-daily-tip {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--main-200);
  border-radius: 10px;
  background: var(--main-30);
  color: var(--gray-800);
  font-size: 13px;
  cursor: pointer;
  text-align: left;

  &:hover {
    border-color: var(--main-color);
  }

  .scene-daily-tip-text {
    flex: 1;
    min-width: 0;
  }

  .scene-daily-tip-name {
    flex-shrink: 0;
    color: var(--gray-500);
  }
}

.scene-group-label {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--gray-500);
}

.scene-template-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
}

.scene-template-card {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-0);
  cursor: pointer;
  text-align: left;

  &:hover {
    border-color: var(--main-400);
    background: var(--main-30);
  }

  .scene-template-icon {
    flex-shrink: 0;
    color: var(--main-color);
  }

  .scene-template-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .scene-template-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--gray-1000);
  }

  .scene-template-desc {
    overflow: hidden;
    font-size: 12px;
    color: var(--gray-600);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}
</style>
