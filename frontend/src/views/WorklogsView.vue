<script setup lang="ts">
/** 工作日志（M3-3）：日期 / 归属项目 / 内容 / 耗时。 */
import BizCrud from '@/components/BizCrud.vue'
import type { Field } from '@/components/biz-types'

const fields: Field[] = [
  { key: 'log_date', label: '日期', type: 'date', width: 130 },
  { key: 'project_id', label: '归属项目', type: 'project', width: 150 },
  { key: 'content', label: '内容', type: 'textarea' },
  { key: 'minutes', label: '耗时(分)', type: 'number', width: 100 },
]
</script>

<template>
  <biz-crud kind="worklogs" title="工作日志" :fields="fields" />
</template>
