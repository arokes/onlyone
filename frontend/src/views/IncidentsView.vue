<script setup lang="ts">
/** 运维记录（M3-3）：系统 / 提出人 / 公司 / 现象 / 根因 / 处置 / 耗时 / 归属项目。
 *
 * 现象 / 根因 / 处置 / 备注 用富文本（type: 'rich'）：存 TipTap 的 ProseMirror JSON，
 * 与记事本同一套结构，支持 Ctrl+V 直接贴截图。其余字段仍是纯文本。
 *
 * 外部跳转：全局搜索点「在运维记录中打开」→ jump store 带 id → 本页把它交给
 * BizCrud，列表就绪后直接打开该条编辑弹窗；也支持深链接 `?tab=incidents&open=<id>`。
 */
import { onMounted, ref } from 'vue'
import BizCrud from '@/components/BizCrud.vue'
import type { Field, FilterSpec } from '@/components/biz-types'
import { incidentCategoryOptions as typeOptions, INCIDENT_OVERDUE_DAYS, incidentRowClass }
  from '@/components/incident-options'
import { useJumpStore } from '@/stores/jump'

const fields: Field[] = [
  { key: 'occurred_at', label: '发生时间', type: 'datetime', width: 160 },
  { key: 'category', label: '类型', type: 'select', width: 100, options: typeOptions },
  { key: 'system', label: '系统', width: 140 },
  { key: 'proposer', label: '提出人', width: 96 },
  { key: 'company', label: '公司', width: 110 },
  { key: 'symptom', label: '现象', type: 'rich', width: 170 },
  { key: 'root_cause', label: '根因', type: 'rich', hideInTable: true },
  { key: 'action', label: '处置', type: 'rich', hideInTable: true },
  {
    key: 'status', label: '状态', type: 'select', width: 100,
    options: [{ label: '未关闭', value: 'open' }, { label: '已关闭', value: 'closed' }],
  },
  { key: 'duration_min', label: '耗时(分)', type: 'number', width: 100 },
  { key: 'project_id', label: '归属项目', type: 'project', width: 140 },
  { key: 'note', label: '备注', type: 'rich', hideInTable: true },
]

/** 列表筛选：关键字全文搜索 + 时间区间 + 类型 + 公司 + 状态 + 项目。
 * 公司候选值不给 options，由 BizCrud 从现有记录去重生成（不引入公司主数据）。 */
const filters: FilterSpec[] = [
  { key: '__keyword', label: '搜索', type: 'keyword',
    placeholder: '全文搜索：现象 / 根因 / 处置 / 提出人 / 备注' },
  { key: 'occurred_at', label: '发生时间', type: 'daterange' },
  { key: 'category', label: '类型', type: 'select', options: typeOptions },
  { key: 'company', label: '公司', type: 'select' },
  {
    key: 'status', label: '状态', type: 'select',
    options: [{ label: '未关闭', value: 'open' }, { label: '已关闭', value: 'closed' }],
  },
  { key: 'project_id', label: '项目', type: 'project' },
]

const jump = useJumpStore()
const openEntryId = ref<number | null>(null)

onMounted(() => {
  const param = new URLSearchParams(window.location.search).get('open')
  if (param && /^\d+$/.test(param)) openEntryId.value = Number(param)
  const pending = jump.consumeIncidentId()
  if (pending) openEntryId.value = pending
})

/** BizCrud 处理完一次打开请求后清空，避免刷新列表时重复弹窗 */
function clearOpen() {
  openEntryId.value = null
}
</script>

<template>
  <div>
    <p class="overdue-hint">
      浅黄行 = 未关闭且挂起超过 {{ INCIDENT_OVERDUE_DAYS }} 天；这些条目同时出现在「概览」的待跟进列表。
    </p>
    <biz-crud
      kind="incidents" title="运维记录" :fields="fields" :filters="filters"
      filter-key="incidents" :row-class="incidentRowClass"
      :open-entry-id="openEntryId" @opened="clearOpen"
    />
  </div>
</template>

<style scoped>
.overdue-hint { margin: 0 0 8px; font-size: 12px; color: var(--ink-3); }
</style>
