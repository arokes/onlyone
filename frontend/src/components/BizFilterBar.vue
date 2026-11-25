<script setup lang="ts">
/**
 * 业务表筛选栏（一行平铺）：关键字 + 若干筛选项 + 清除 + 命中计数。
 *
 * 只负责渲染与回传筛选值，不持有数据——过滤逻辑集中在 BizCrud，
 * 避免"控件在子组件、过滤在父组件"时两边各算一遍。
 */
import { computed } from 'vue'
import { NButton, NDatePicker, NInput, NSelect } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import type { FilterSpec } from './biz-types'

const props = defineProps<{
  filters: FilterSpec[]
  values: Record<string, any>
  /** select / project 的候选值（由 BizCrud 从现有行或项目列表生成） */
  options: Record<string, SelectOption[]>
  matched: number
  total: number
}>()
const emit = defineEmits<{ (e: 'update', patch: Record<string, any>): void }>()

function set(key: string, value: any) {
  emit('update', { [key]: value })
}

const active = computed(() => props.filters.some(f => {
  const v = props.values[f.key]
  if (Array.isArray(v)) return v.length > 0
  return v !== null && v !== undefined && String(v).trim() !== ''
}))

/** 区间拆成两个单日期控件：每个只点一次即可提交，也能只填一端。值仍是 [起, 止]。 */
function rangeAt(key: string, index: 0 | 1): string | null {
  const value = props.values[key]
  return Array.isArray(value) ? (value[index] ?? null) : null
}

function setRange(key: string, index: 0 | 1, value: string | null) {
  const current = Array.isArray(props.values[key]) ? [...props.values[key]] : [null, null]
  current[index] = value ?? null
  const [start, end] = current
  emit('update', { [key]: start || end ? current : null })
}
</script>

<template>
  <div class="filter-bar">
    <template v-for="f in filters" :key="f.key">
      <n-input
        v-if="f.type === 'keyword' || f.type === 'text'"
        :value="values[f.key] ?? ''"
        size="small" clearable :placeholder="f.placeholder || f.label"
        :style="{ width: f.type === 'keyword' ? '220px' : '150px' }"
        @update:value="set(f.key, $event)"
      />
      <div v-else-if="f.type === 'daterange'" class="filter-range">
        <n-date-picker
          :formatted-value="rangeAt(f.key, 0)"
          value-format="yyyy-MM-dd" type="date" size="small" clearable
          :placeholder="f.placeholder || '开始日期'" style="width: 124px"
          @update:formatted-value="setRange(f.key, 0, $event)"
        />
        <span class="range-sep">~</span>
        <n-date-picker
          :formatted-value="rangeAt(f.key, 1)"
          value-format="yyyy-MM-dd" type="date" size="small" clearable
          placeholder="结束日期" style="width: 124px"
          @update:formatted-value="setRange(f.key, 1, $event)"
        />
      </div>
      <n-select
        v-else
        :value="values[f.key] ?? null"
        :options="options[f.key] || []"
        :placeholder="f.label"
        size="small" clearable filterable
        :style="{ width: f.type === 'project' ? '160px' : '140px' }"
        @update:value="set(f.key, $event ?? null)"
      />
    </template>

    <n-button size="small" quaternary :disabled="!active" @click="emit('update', {
      __reset: true,
    })">
      清除筛选
    </n-button>
    <span v-if="active" class="filter-count">筛选后 {{ matched }} / 共 {{ total }}</span>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin-bottom: 10px; padding: 8px 10px;
  background: var(--paper-2); border: 1px solid var(--line); border-radius: var(--radius-sm);
}
.filter-count { font-size: 12px; color: var(--ink-3); margin-left: auto; }
.filter-range { display: inline-flex; align-items: center; gap: 4px; }
.range-sep { color: var(--ink-3); font-size: 12px; }
</style>
