<script setup lang="ts">
/**
 * 四张业务表共用的 CRUD 组件（M3-3；M5-5 增加多选与列复制）：
 * 表格 + 表单 + 删除确认 + 行多选 / 批量删除 / TSV 列复制。
 * 字段由 Field[] 配置驱动，避免四个页面重复实现。
 */
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NButton, NDataTable, NModal, useDialog, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { api } from '@/api/client'
import BizFieldInput from './BizFieldInput.vue'
import type { Field } from './biz-types'

const props = defineProps<{
  kind: string
  title: string
  fields: Field[]
  /** 是否追加「关联条目」操作（项目事项页用） */
  extra?: boolean
}>()
const emit = defineEmits<{ (e: 'extra', row: Record<string, any>): void }>()

const message = useMessage()
const dialog = useDialog()
const rows = ref<Record<string, any>[]>([])
const projects = ref<Record<string, any>[]>([])
const loading = ref(false)
const showModal = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<Record<string, any>>({})
const checkedRowKeys = ref<(string | number)[]>([])

const projectOptions = computed(() =>
  projects.value.map(p => ({ label: String(p.name), value: p.id as number })),
)

/** 与后端 business._validate 对齐的必填规则：保存前本地即时提示，不等后端返回错误 */
const REQUIRED: Record<string, { key: string; label: string }[]> = {
  projects: [{ key: 'name', label: '项目名称' }],
  links: [{ key: 'title', label: '链接标题' }, { key: 'url', label: '链接地址' }],
  worklogs: [{ key: 'log_date', label: '日志日期' }],
  incidents: [{ key: 'occurred_at', label: '发生时间' }],
}
const requiredFields = computed(() => REQUIRED[props.kind] ?? [])
function isRequired(key: string): boolean {
  return requiredFields.value.some(f => f.key === key)
}
function validate(): string | null {
  for (const f of requiredFields.value) {
    if (!String(form[f.key] ?? '').trim()) return `${f.label}不能为空`
  }
  if (props.kind === 'links') {
    const url = String(form.url ?? '').trim()
    if (url && !/^https?:\/\//i.test(url)) return '链接必须以 http:// 或 https:// 开头'
  }
  return null
}

function today(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function nowText(): string {
  const d = new Date()
  return `${today()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function blank(): Record<string, any> {
  const data: Record<string, any> = {}
  for (const f of props.fields) {
    if (f.type === 'date') data[f.key] = today()
    else if (f.type === 'datetime') data[f.key] = nowText()
    else if (f.type === 'select' && f.options?.length) data[f.key] = f.options[0].value
    else data[f.key] = null
  }
  return data
}

async function load() {
  loading.value = true
  try {
    rows.value = await api<Record<string, any>[]>('/api/biz/' + props.kind)
    projects.value = props.kind === 'projects'
      ? rows.value
      : await api<Record<string, any>[]>('/api/biz/projects')
  } catch (e) {
    message.error(String(e))
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editingId.value = null
  Object.keys(form).forEach(k => delete form[k])
  Object.assign(form, blank())
  showModal.value = true
}

function openEdit(row: Record<string, any>) {
  editingId.value = row.id
  Object.keys(form).forEach(k => delete form[k])
  Object.assign(form, blank())
  for (const f of props.fields) form[f.key] = row[f.key] ?? null
  showModal.value = true
}

async function save() {
  const invalid = validate()
  if (invalid) {
    message.warning(invalid)
    return
  }
  saving.value = true
  try {
    const data: Record<string, any> = {}
    for (const f of props.fields) data[f.key] = form[f.key] ?? null
    const path = '/api/biz/' + props.kind + (editingId.value ? '/' + editingId.value : '')
    await api(path, {
      method: editingId.value ? 'PUT' : 'POST',
      body: JSON.stringify({ data }),
    })
    showModal.value = false
    message.success('已保存')
    await load()
  } catch (e) {
    message.error(String(e))
  } finally {
    saving.value = false
  }
}

function remove(row: Record<string, any>) {
  dialog.warning({
    title: '确认删除',
    content: '删除后该记录的检索条目与关联关系会一并移除。',
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api(`/api/biz/${props.kind}/${row.id}`, { method: 'DELETE' })
        message.success('已删除')
        await load()
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

function batchRemove() {
  const ids = [...checkedRowKeys.value]
  if (!ids.length) return
  dialog.warning({
    title: '批量删除',
    content: `确认删除选中的 ${ids.length} 条记录？检索条目与关联关系会一并移除。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        for (const id of ids) {
          await api(`/api/biz/${props.kind}/${id}`, { method: 'DELETE' })
        }
        checkedRowKeys.value = []
        message.success(`已删除 ${ids.length} 条`)
        await load()
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

function cellValue(row: Record<string, any>, f: Field): string {
  const raw = row[f.key]
  if (f.type === 'project') {
    const p = projects.value.find(x => x.id === raw)
    return p ? String(p.name) : '—'
  }
  if (f.type === 'select') {
    const o = f.options?.find(x => x.value === raw)
    return o ? o.label : (raw == null || raw === '' ? '—' : String(raw))
  }
  if (raw == null || raw === '') return '—'
  const text = String(raw)
  return f.type === 'textarea' && text.length > 60 ? text.slice(0, 60) + '…' : text
}

function toTsv(list: Record<string, any>[]): string {
  const cols = props.fields.filter(f => !f.hideInTable)
  const lines = [cols.map(c => c.label).join('\t')]
  for (const row of list) {
    lines.push(cols
      .map(c => cellValue(row, c).replace(/[\t\r\n]/g, ' '))
      .join('\t'))
  }
  return lines.join('\n')
}

async function writeClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  document.execCommand('copy')
  document.body.removeChild(ta)
}

async function copyRows() {
  const selected = checkedRowKeys.value
  const list = selected.length
    ? rows.value.filter(r => selected.includes(r.id))
    : rows.value
  if (!list.length) {
    message.warning('没有可复制的数据')
    return
  }
  try {
    await writeClipboard(toTsv(list))
    message.success(`已复制 ${list.length} 行（TSV，可直接粘贴到 Excel）`)
  } catch (e) {
    message.error('复制失败：' + String(e))
  }
}

async function openLink(url: string) {
  if (!url) return
  try {
    await api('/api/open-link', { method: 'POST', body: JSON.stringify({ url }) })
    message.success('已用默认浏览器打开')
  } catch (e) {
    message.error(String(e))
  }
}

function renderCell(f: Field, row: Record<string, any>) {
  const raw = row[f.key]
  if (f.type === 'url') {
    return h('a', { class: 'cell-link', onClick: () => openLink(String(raw || '')) },
             String(raw || '—'))
  }
  return cellValue(row, f)
}

const columns = computed<DataTableColumns>(() => {
  const cols: DataTableColumns = [{ type: 'selection' }]
  for (const f of props.fields.filter(x => !x.hideInTable)) {
    cols.push({
      title: f.label,
      key: f.key,
      width: f.width,
      ellipsis: { tooltip: true },
      render: (row: any) => renderCell(f, row),
    })
  }
  cols.push({
    title: '操作',
    key: '__actions',
    width: props.extra ? 230 : 160,
    fixed: 'right',
    render: (row: any) => h('div', { class: 'row' }, [
      h(NButton, { size: 'tiny', secondary: true, onClick: () => openEdit(row) },
        { default: () => '编辑' }),
      h(NButton, { size: 'tiny', quaternary: true, type: 'error', onClick: () => remove(row) },
        { default: () => '删除' }),
      ...(props.extra ? [h(NButton, { size: 'tiny', quaternary: true, onClick: () => emit('extra', row) },
        { default: () => '关联条目' })] : []),
    ]),
  })
  return cols
})
</script>

<template>
  <div class="card">
    <div class="row" style="margin-bottom: 12px">
      <h3 class="section-title">{{ title }}</h3>
      <span class="count-chip">{{ rows.length }}</span>
      <div class="grow" />
      <n-button size="small" @click="load">刷新</n-button>
      <n-button size="small" :disabled="!checkedRowKeys.length" @click="copyRows">
        复制表格（TSV）
      </n-button>
      <n-button size="small" :disabled="!checkedRowKeys.length" type="error" quaternary
                @click="batchRemove">
        批量删除{{ checkedRowKeys.length ? ` (${checkedRowKeys.length})` : '' }}
      </n-button>
      <n-button size="small" type="primary" @click="openCreate">新增</n-button>
    </div>
    <div class="muted" style="margin-bottom: 10px">
      勾选行后可批量删除或按列复制；未勾选时「复制表格」导出当前全部行（TSV，可直接粘贴到 Excel）。
    </div>
    <n-data-table
      v-model:checked-row-keys="checkedRowKeys"
      :row-key="(row: any) => row.id"
      :columns="columns" :data="rows" :loading="loading"
      :bordered="false" size="small" :scroll-x="1020"
      :pagination="{ pageSize: 10 }"
    />
  </div>

  <n-modal
    v-model:show="showModal" preset="card"
    :title="(editingId ? '编辑' : '新增') + title" style="width: 580px"
  >
    <div v-for="f in fields" :key="f.key" class="form-row">
      <label class="form-label">
        {{ f.label }}<span v-if="isRequired(f.key)" class="req" title="必填">*</span>
      </label>
      <div class="grow">
        <biz-field-input
          :field="f" :model-value="form[f.key]"
          :project-options="projectOptions" @update:model-value="form[f.key] = $event"
        />
      </div>
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showModal = false">取消</n-button>
        <n-button type="primary" :loading="saving" @click="save">保存</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.form-row { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.form-label { width: 88px; flex-shrink: 0; padding-top: 6px; font-size: 13px; color: var(--ink-2); }
.req { color: var(--danger); margin-left: 3px; }
.form-row > :not(.form-label) { flex: 1; min-width: 0; }
:deep(.cell-link) { color: var(--info-deep); cursor: pointer; text-decoration: underline; }
</style>
