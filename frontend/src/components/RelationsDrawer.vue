<script setup lang="ts">
/** 项目关联条目抽屉（M3-4）：查看关联、解除关联、跳回全局搜索查看条目。 */
import { ref, watch } from 'vue'
import { NButton, NDrawer, NDrawerContent, NTag, useMessage } from 'naive-ui'
import { api } from '@/api/client'
import { useJumpStore } from '@/stores/jump'

interface Relation {
  src_entry_id: number
  dst_entry_id: number
  relation: string
  peer_id: number
  peer_type: string
  peer_title: string
  peer_summary: string
}

const TYPE_LABEL: Record<string, string> = {
  file: '文件', project: '项目事项', worklog: '工作日志',
  incident: '运维记录', link: '企微链接',
}

const props = defineProps<{ show: boolean; row: Record<string, any> | null }>()
const emit = defineEmits<{ (e: 'update:show', value: boolean): void }>()
const jump = useJumpStore()
const message = useMessage()
const rels = ref<Relation[]>([])
const busy = ref(false)

async function load() {
  if (!props.row?.entry_id) return
  busy.value = true
  try {
    rels.value = await api<Relation[]>(`/api/relations?entry_id=${props.row.entry_id}`)
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

watch([() => props.show, () => props.row?.entry_id], () => {
  if (props.show) load()
}, { immediate: true })

async function unlink(r: Relation) {
  try {
    const q = `src_entry_id=${r.src_entry_id}&dst_entry_id=${r.dst_entry_id}&relation=${encodeURIComponent(r.relation)}`
    await api('/api/relations?' + q, { method: 'DELETE' })
    message.success('已解除关联')
    await load()
  } catch (e) {
    message.error(String(e))
  }
}

function goto(r: Relation) {
  // 带上对端标题：关掉预览后搜索页仍有这组结果，不会落回空态
  jump.jumpToEntry(r.peer_id, r.peer_title)
  emit('update:show', false)
}
</script>

<template>
  <n-drawer :show="show" :width="520" placement="right" @update:show="emit('update:show', $event)">
    <n-drawer-content :title="`关联条目 · ${row?.name ?? ''}`" closable>
      <div v-if="busy" class="muted">加载中…</div>
      <div v-else-if="!rels.length" class="muted">
        还没有关联条目。可在全局搜索结果中点「关联到项目」把文件或其他记录关联过来。
      </div>
      <div v-for="r in rels" :key="r.peer_id + r.relation" class="rel-row">
        <div class="row">
          <n-tag size="small" :bordered="false">{{ TYPE_LABEL[r.peer_type] ?? r.peer_type }}</n-tag>
          <span class="grow ellipsis">{{ r.peer_title }}</span>
        </div>
        <div class="muted ellipsis rel-summary">{{ r.peer_summary || '—' }}</div>
        <div class="row" style="margin-top: 6px">
          <n-button size="tiny" secondary @click="goto(r)">在搜索中查看</n-button>
          <n-button size="tiny" quaternary @click="unlink(r)">解除关联</n-button>
        </div>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.rel-row { padding: 10px 0; border-top: 1px solid var(--line-soft); }
.rel-row:first-of-type { border-top: none; }
.rel-summary { font-size: 12px; margin-top: 6px; }
</style>
