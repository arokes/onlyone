<script setup lang="ts">
/**
 * 统一空状态（v0.9 规范 §8.7）：
 * 四要素 = 这页干什么 + 怎么产生第一条 + 唯一主按钮 + 一条替代路径。
 * 不放插画：线性图标 48px，与侧栏图标同一笔触（stroke 1.5）。
 */
import { NButton } from 'naive-ui'

withDefaults(defineProps<{
  title: string
  desc?: string
  actionText?: string
  hint?: string
  /** 紧凑模式：放在表格 / 抽屉内部时用，高度从 220 降到 140 */
  compact?: boolean
}>(), { compact: false })

const emit = defineEmits<{ (e: 'action'): void }>()
</script>

<template>
  <div class="empty" :class="{ compact }">
    <svg class="empty-icon" viewBox="0 0 48 48" fill="none" stroke="currentColor"
         stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M9 15.5 24 8l15 7.5v17L24 40 9 32.5z" />
      <path d="M9 15.5 24 23l15-7.5" />
      <path d="M24 23v17" />
    </svg>
    <div class="empty-title">{{ title }}</div>
    <div v-if="desc" class="empty-desc">{{ desc }}</div>
    <div v-if="actionText || hint" class="empty-actions">
      <n-button v-if="actionText" type="primary" size="small" @click="emit('action')">
        {{ actionText }}
      </n-button>
      <span v-if="hint" class="empty-hint">{{ hint }}</span>
    </div>
  </div>
</template>

<style scoped>
.empty {
  min-height: 220px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px;
  text-align: center;
}
.empty.compact { min-height: 140px; gap: 6px; }
.empty-icon { width: 48px; height: 48px; color: var(--ink-3); opacity: 0.55; }
.empty.compact .empty-icon { width: 36px; height: 36px; }
.empty-title { font-size: 14px; font-weight: 500; color: var(--ink); }
.empty-desc {
  max-width: 340px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ink-2);
}
.empty-actions { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
.empty-hint { font-size: 12px; color: var(--ink-3); }
</style>
