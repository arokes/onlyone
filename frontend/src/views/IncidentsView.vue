<script setup lang="ts">
/** 运维记录（M3-3）：系统 / 现象 / 根因 / 处置 / 耗时 / 归属项目。 */
import BizCrud from '@/components/BizCrud.vue'
import type { Field } from '@/components/biz-types'

const fields: Field[] = [
  { key: 'occurred_at', label: '发生时间', type: 'datetime', width: 160 },
  { key: 'system', label: '系统', width: 140 },
  { key: 'symptom', label: '现象', type: 'textarea', width: 200 },
  { key: 'root_cause', label: '根因', type: 'textarea', hideInTable: true },
  { key: 'action', label: '处置', type: 'textarea', hideInTable: true },
  {
    key: 'status', label: '状态', type: 'select', width: 100,
    options: [{ label: '未关闭', value: 'open' }, { label: '已关闭', value: 'closed' }],
  },
  { key: 'duration_min', label: '耗时(分)', type: 'number', width: 100 },
  { key: 'project_id', label: '归属项目', type: 'project', width: 140 },
  { key: 'note', label: '备注', type: 'textarea', hideInTable: true },
]
</script>

<template>
  <biz-crud kind="incidents" title="运维记录" :fields="fields" />
</template>
