<script setup lang="ts">
/** 项目事项（M3-3）：CRUD + 查看该项目下关联的文件/日志/运维/链接。 */
import { ref } from 'vue'
import BizCrud from '@/components/BizCrud.vue'
import RelationsDrawer from '@/components/RelationsDrawer.vue'
import type { Field } from '@/components/biz-types'

const fields: Field[] = [
  { key: 'name', label: '项目名称', width: 190 },
  {
    key: 'status', label: '状态', type: 'select', width: 110,
    options: [{ label: '进行中', value: 'active' }, { label: '已归档', value: 'archived' }],
  },
  { key: 'note', label: '说明', type: 'textarea' },
]

const selected = ref<Record<string, any> | null>(null)
const showRelations = ref(false)
</script>

<template>
  <div>
    <biz-crud
      kind="projects" title="项目事项" :fields="fields" extra
      @extra="(r) => { selected = r; showRelations = true }"
    />
    <relations-drawer v-model:show="showRelations" :row="selected" />
  </div>
</template>
