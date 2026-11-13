<script setup lang="ts">
/** 首次启动向导（M5-3）：设置索引根目录与爱数同步盘目录，可跳过。 */
import { ref } from 'vue'
import { NButton, NInput, NModal, useMessage } from 'naive-ui'
import { api } from '@/api/client'

const SKIP_KEY = 'onlyone_setup_skipped'
const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ (e: 'update:show', value: boolean): void }>()
const message = useMessage()
const localPath = ref('')
const syncPath = ref('')
const busy = ref(false)

async function addRoot(path: string, kind: string) {
  await api('/api/roots', {
    method: 'POST',
    body: JSON.stringify({ path, kind }),
  })
}

function close() {
  localStorage.setItem(SKIP_KEY, '1')
  emit('update:show', false)
}

async function finish() {
  const local = localPath.value.trim()
  const sync = syncPath.value.trim()
  if (!local && !sync) {
    message.warning('请至少填写一个目录，或点「跳过」')
    return
  }
  busy.value = true
  try {
    if (local) await addRoot(local, 'local')
    if (sync) await addRoot(sync, 'anyshare_sync')
    close()
    message.success('已登记目录，请在「索引目录」页点击扫描或重建索引')
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <n-modal
    :show="props.show" preset="card" title="首次启动向导" style="width: 580px"
    :mask-closable="false" :closable="false"
  >
    <div class="muted" style="margin-bottom: 14px">
      首次使用需要指定要索引的本地目录。爱数同步盘请先在客户端设置好本地同步目录，再把该目录填在下面；
      程序不接触企业令牌，也不越权访问云端。
    </div>
    <div class="form-row">
      <label class="form-label">本地索引目录</label>
      <n-input v-model:value="localPath" placeholder="例如 D:\Work\运维" />
    </div>
    <div class="form-row">
      <label class="form-label">爱数同步盘</label>
      <n-input v-model:value="syncPath" placeholder="选填：爱数客户端的本地同步目录" />
    </div>
    <div class="muted">
      后续可随时在「索引目录」页增删目录；程序启动后会自动监听已登记目录的变化。
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button quaternary @click="close">跳过</n-button>
        <n-button type="primary" :loading="busy" @click="finish">完成设置</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.form-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.form-label { width: 110px; flex-shrink: 0; font-size: 13px; color: var(--ink-2); }
.form-row > :not(.form-label) { flex: 1; min-width: 0; }
</style>
