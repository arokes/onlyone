<script setup lang="ts">
defineProps<{ title: string; stage: string }>()
</script>

<template>
  <div class="card">
    <h3 style="font-size: 14px; font-weight: 500">{{ title }}</h3>
    <p class="muted" style="margin-top: 10px">该页签计划在 {{ stage || '后续阶段' }} 实现，M1 仅提供导航占位。</p>
    <p class="muted" style="margin-top: 6px">当前可用功能：全局搜索（文件名）、索引目录（添加 / 扫描 / 移除）。</p>
  </div>
</template>
