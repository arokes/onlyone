<script setup lang="ts">
/** 单个业务字段的输入控件（从 BizCrud 拆出，避免 v-for + v-else-if 的模板作用域问题）。 */
import { computed } from 'vue'
import { NDatePicker, NInput, NInputNumber, NSelect } from 'naive-ui'
import type { SelectOption } from 'naive-ui'
import type { Field } from './biz-types'

const props = defineProps<{
  field: Field
  modelValue: any
  projectOptions?: { label: string; value: number }[]
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: any): void }>()
function set(value: any) { emit('update:modelValue', value) }

const selectOptions = computed<SelectOption[]>(() =>
  (props.field.options || []).map(o => ({ label: o.label, value: o.value })),
)
</script>

<template>
  <n-input
    v-if="field.type === 'textarea'" :value="modelValue"
    type="textarea" :rows="3" @update:value="set"
  />
  <n-input
    v-else-if="field.type === 'url'" :value="modelValue"
    placeholder="https://..." @update:value="set"
  />
  <n-input-number
    v-else-if="field.type === 'number'" :value="modelValue"
    style="width: 100%" @update:value="set"
  />
  <n-date-picker
    v-else-if="field.type === 'date'" :formatted-value="modelValue"
    value-format="yyyy-MM-dd" type="date" style="width: 100%" clearable
    @update:formatted-value="set"
  />
  <n-select
    v-else-if="field.type === 'project'" :value="modelValue"
    :options="projectOptions || []" clearable placeholder="（不归属项目）" @update:value="set"
  />
  <n-select
    v-else-if="field.type === 'select'" :value="modelValue"
    :options="selectOptions" @update:value="set"
  />
  <n-input
    v-else :value="modelValue"
    :placeholder="field.type === 'datetime' ? '2026-11-06 09:00' : ''"
    @update:value="set"
  />
</template>
