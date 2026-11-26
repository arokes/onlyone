<script setup lang="ts">
/**
 * 四张业务表共用的 CRUD 组件（M3-3；M5-5 增加多选与列复制）：
 * 表格 + 表单 + 删除确认 + 行多选 / 批量删除 / TSV 列复制。
 * 字段由 Field[] 配置驱动，避免四个页面重复实现。
 */
import { computed, h, onMounted, reactive, ref, watch } from 'vue'
import { NButton, NDataTable, NDynamicTags, NInput, NModal, NSelect, useDialog, useMessage } from 'naive-ui'
import type { DataTableColumns, SelectOption } from 'naive-ui'
import { api } from '@/api/client'
import BizFieldInput from './BizFieldInput.vue'
import BizFilterBar from './BizFilterBar.vue'
import EmptyState from './EmptyState.vue'
import type { Field, FilterSpec } from './biz-types'
import { useViewStateStore } from '@/stores/viewState'
import { countImages, docToText } from '@/utils/richtext'
import { parseTags } from '@/utils/tags'

const props = defineProps<{
  kind: string
  title: string
  fields: Field[]
  /** 是否追加「关联条目」操作（项目事项页用） */
  extra?: boolean
  /** 限定并固定归属项目：列表按 project_id 过滤，新增时强制写入该值 */
  projectId?: number | null
  /** 空状态补充说明（不给则用通用文案） */
  emptyDesc?: string
  /** 外部指定要打开的记录 id（全局搜索 → 运维记录）：列表就绪后打开编辑弹窗 */
  openEntryId?: number | null
  /** 列表筛选声明（不给则没有筛选栏） */
  filters?: FilterSpec[]
  /** 筛选记忆键（一般传表 kind）：给了就跨页签保留筛选条件 */
  filterKey?: string
  /** 行级样式钩子（运维记录用：未关闭且超期的行标黄）；返回 class 字符串 */
  rowClass?: (row: Record<string, any>) => string
}>()
const emit = defineEmits<{
  (e: 'extra', row: Record<string, any>): void
  /** 已处理完一次外部打开请求，父级可清空 openEntryId */
  (e: 'opened'): void
}>()

const message = useMessage()
const dialog = useDialog()
const viewState = useViewStateStore()
const rows = ref<Record<string, any>[]>([])
const projects = ref<Record<string, any>[]>([])
const loading = ref(false)
const showModal = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<Record<string, any>>({})
/** 用户标签不属业务表，单独维护；保存时写 /api/entries/{id}/tags */
const formTags = ref<string[]>([])
const checkedRowKeys = ref<(string | number)[]>([])

const projectOptions = computed(() =>
  projects.value.map(p => ({ label: String(p.name), value: p.id as number })),
)

// ---------------------------------------------------------------- 列表筛选
/** 筛选值：keyword/text 为字符串，select/project 为标量，daterange 为 [起, 止] */
const filterValues = reactive<Record<string, any>>({})

function resetFilterValues() {
  for (const spec of props.filters || []) {
    filterValues[spec.key] = spec.type === 'daterange' ? null : (spec.type === 'keyword' || spec.type === 'text' ? '' : null)
  }
}
watch(() => props.filters, () => {
  resetFilterValues()
  // 切页签回来时恢复上次的筛选条件（键由父级指定，如 'incidents'）
  if (!props.filterKey) return
  const saved = viewState.filtersFor(props.filterKey)
  if (!saved) return
  for (const spec of props.filters || []) {
    if (spec.key in saved) filterValues[spec.key] = saved[spec.key]
  }
}, { immediate: true })

/** select 类筛选项的候选值：显式 options 优先，否则从现有行去重（公司就是这条路） */
const filterOptions = computed<Record<string, SelectOption[]>>(() => {
  const out: Record<string, SelectOption[]> = {}
  for (const spec of props.filters || []) {
    if (spec.type === 'project') {
      out[spec.key] = projectOptions.value as SelectOption[]
      continue
    }
    if (spec.type !== 'select') continue
    if (spec.options?.length) {
      out[spec.key] = spec.options.map(o => ({ label: o.label, value: String(o.value) }))
      continue
    }
    const seen = new Set<string>()
    for (const row of rows.value) {
      const value = String(row[spec.key] ?? '').trim()
      if (value) seen.add(value)
    }
    out[spec.key] = [...seen].sort((a, b) => a.localeCompare(b, 'zh-CN'))
      .map(v => ({ label: v, value: v }))
  }
  return out
})

const filtersActive = computed(() => (props.filters || []).some(spec => {
  const v = filterValues[spec.key]
  if (Array.isArray(v)) return v.length > 0
  return v !== null && v !== undefined && String(v).trim() !== ''
}))

/** 全文匹配的取样：富文本先抽纯文本，图片不产生字符 */
function haystack(row: Record<string, any>): string {
  const parts: string[] = []
  for (const f of props.fields) {
    const raw = row[f.key]
    if (raw == null || raw === '') continue
    parts.push(f.type === 'rich' ? docToText(raw) : String(raw))
  }
  return parts.join('\n').toLowerCase()
}

const filteredRows = computed(() => {
  const list = props.filters || []
  if (!list.length) return rows.value
  const active = list.filter(spec => {
    const v = filterValues[spec.key]
    if (Array.isArray(v)) return v.length > 0
    return v !== null && v !== undefined && String(v).trim() !== ''
  })
  if (!active.length) return rows.value

  const haystacks = new Map<number, string>()
  return rows.value.filter(row => active.every(spec => {
    const value = filterValues[spec.key]
    if (spec.type === 'keyword') {
      const terms = String(value).toLowerCase().split(/\s+/).filter(Boolean)
      let hay = haystacks.get(row.id)
      if (hay === undefined) {
        hay = haystack(row)
        haystacks.set(row.id, hay)
      }
      return terms.every(term => hay.includes(term))
    }
    if (spec.type === 'daterange') {
      const day = String(row[spec.key] ?? '').slice(0, 10)
      const [start, end] = value as [string, string]
      if (!day) return false
      return (!start || day >= start) && (!end || day <= end)
    }
    if (spec.type === 'project') {
      return Number(row[spec.key]) === Number(value)
    }
    if (spec.type === 'text') {
      return String(row[spec.key] ?? '').toLowerCase()
        .includes(String(value).toLowerCase())
    }
    return String(row[spec.key] ?? '') === String(value)
  }))
})

/** 筛选条件变化后清掉勾选（被筛掉的行不该继续参与批量删除），并记住条件 */
watch(filterValues, () => {
  checkedRowKeys.value = []
  if (props.filterKey) viewState.setFilters(props.filterKey, { ...filterValues })
}, { deep: true })

function onFilterUpdate(patch: Record<string, any>) {
  if (patch.__reset) {
    resetFilterValues()
    return
  }
  Object.assign(filterValues, patch)
}

// ---------------------------------------------------------------- 保存视图
/** 视图存的是筛选值（不是结果），恢复时按当前数据重算，见 backend/onlyone/saved_views.py */
interface SavedView { id: string; name: string; tab: string; filters: Record<string, any> }
const views = ref<SavedView[]>([])
const activeView = ref<string | null>(null)
const showViewModal = ref(false)
const viewName = ref('')

const viewOptions = computed(() => views.value.map(v => ({ label: v.name, value: v.id })))

async function loadViews() {
  if (!props.filterKey) return
  try {
    views.value = await api<SavedView[]>('/api/views?tab=' + props.kind)
  } catch {
    // 视图读不出来不影响列表本身，静默即可
  }
}

function applyView(id: string | null) {
  activeView.value = id
  resetFilterValues()
  if (!id) return
  const view = views.value.find(v => v.id === id)
  if (!view) return
  for (const spec of props.filters || []) {
    if (spec.key in (view.filters || {})) filterValues[spec.key] = view.filters[spec.key]
  }
}

/** 用当前条件拼一个默认名称，省得每次手输 */
function defaultViewName(): string {
  const parts: string[] = []
  for (const spec of props.filters || []) {
    if (spec.type === 'keyword' || spec.type === 'text') continue
    const value = filterValues[spec.key]
    if (value == null || value === '') continue
    if (Array.isArray(value)) {
      const kept = value.filter(Boolean)
      if (kept.length) parts.push(kept.join(' ~ '))
    } else {
      const option = spec.options?.find(o => o.value === value)
      parts.push(option ? option.label : String(value))
    }
    if (parts.length >= 3) break
  }
  return parts.slice(0, 3).join(' · ') || '我的视图'
}

function openSaveView() {
  viewName.value = defaultViewName()
  showViewModal.value = true
}

async function commitView() {
  const name = viewName.value.trim()
  if (!name) {
    message.warning('请输入视图名称')
    return
  }
  const filters: Record<string, any> = {}
  for (const [key, value] of Object.entries(filterValues)) {
    if (value == null || value === '') continue
    if (Array.isArray(value)) {
      const kept = value.filter(Boolean)
      if (!kept.length) continue
      filters[key] = kept
    } else {
      filters[key] = value
    }
  }
  try {
    const created = await api<SavedView>('/api/views', {
      method: 'POST',
      body: JSON.stringify({ name, tab: props.kind, filters }),
    })
    showViewModal.value = false
    await loadViews()
    activeView.value = created.id
    message.success('已保存视图')
  } catch (e) {
    message.error(String(e))
  }
}

function removeView() {
  const item = views.value.find(v => v.id === activeView.value)
  if (!item) return
  dialog.warning({
    title: '删除视图',
    content: `确认删除「${item.name}」？只删这组筛选条件，不影响任何记录。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api('/api/views/' + item.id, { method: 'DELETE' })
        activeView.value = null
        await loadViews()
        message.success('已删除视图')
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

/** 已固定归属项目时，归属列既不显示也不让选，避免误改到别的项目 */
const fixedProject = computed(() => typeof props.projectId === 'number' && props.projectId > 0)
const visibleFields = computed(() =>
  fixedProject.value ? props.fields.filter(f => f.key !== 'project_id') : props.fields)

/** 与后端 business._validate 对齐的必填规则：保存前本地即时提示，不等后端返回错误 */
const REQUIRED: Record<string, { key: string; label: string }[]> = {
  projects: [{ key: 'name', label: '项目名称' }],
  links: [{ key: 'title', label: '链接标题' }, { key: 'url', label: '链接地址' }],
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
  if (fixedProject.value) data.project_id = props.projectId
  return data
}

async function load() {
  loading.value = true
  try {
    const suffix = fixedProject.value ? `?project_id=${props.projectId}` : ''
    rows.value = await api<Record<string, any>[]>('/api/biz/' + props.kind + suffix)
    if (props.kind === 'projects') {
      projects.value = rows.value
    } else if (!fixedProject.value) {
      projects.value = await api<Record<string, any>[]>('/api/biz/projects')
    }
  } catch (e) {
    message.error(String(e))
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  load()
  loadViews()
})
watch(() => props.projectId, () => {
  checkedRowKeys.value = []
  load()
})

/**
 * 外部打开请求（全局搜索「在运维记录中打开」/ 深链接 ?open=）：
 * 等行数据就绪后按 id 定位并打开编辑弹窗，完成后 emit opened 让父级清空。
 * `handledOpenId` 防止列表刷新时重复弹窗；父级清空（传回 null）时复位。
 */
let handledOpenId: number | null = null
watch([() => props.openEntryId, rows, loading], () => {
  const target = props.openEntryId ?? null
  if (target == null) { handledOpenId = null; return }
  if (loading.value || handledOpenId === target) return
  const row = rows.value.find(r => r.id === target)
  handledOpenId = target
  if (row) openEdit(row)
  else message.warning('该记录可能已被删除，或不在当前项目下')
  emit('opened')
})

function openCreate() {
  editingId.value = null
  Object.keys(form).forEach(k => delete form[k])
  Object.assign(form, blank())
  formTags.value = []
  showModal.value = true
}

function openEdit(row: Record<string, any>) {
  editingId.value = row.id
  Object.keys(form).forEach(k => delete form[k])
  Object.assign(form, blank())
  for (const f of props.fields) form[f.key] = row[f.key] ?? null
  formTags.value = parseTags(row.user_tags)
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
    if (fixedProject.value) data.project_id = props.projectId
    const path = '/api/biz/' + props.kind + (editingId.value ? '/' + editingId.value : '')
    const saved = await api<{ id: number; entry_id: number }>(path, {
      method: editingId.value ? 'PUT' : 'POST',
      body: JSON.stringify({ data }),
    })
    // 标签是挂在 entries 上的，业务记录写完再单独提交；失败不回滚记录，
    // 但要明确告知，避免用户以为标签存住了。
    if (saved?.entry_id) {
      try {
        await api('/api/entries/' + saved.entry_id + '/tags', {
          method: 'PUT',
          body: JSON.stringify({ tags: formTags.value }),
        })
      } catch (e) {
        message.warning('记录已保存，但标签未能写入：' + String(e))
      }
    }
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
  // 富文本列存的是 ProseMirror JSON：只取纯文本，图片另记张数，
  // 否则表格里会出现一整串 {"type":"doc"...}
  if (f.type === 'rich') {
    const text = docToText(raw).replace(/\s+/g, ' ').trim()
    const images = countImages(raw)
    if (!text && !images) return '—'
    const cut = text.length > 60 ? text.slice(0, 60) + '…' : text
    // 图片标记放前面：放末尾会被列宽省略号吃掉，等于没提示
    return images ? (cut ? `〔${images} 图〕 ${cut}` : `〔${images} 图〕`) : cut
  }
  if (raw == null || raw === '') return '—'
  const text = String(raw)
  return f.type === 'textarea' && text.length > 60 ? text.slice(0, 60) + '…' : text
}

/** 列表里的标签单元格：最多两枚，多余折成 +N */
function renderTags(row: Record<string, any>) {
  const tags = parseTags(row.user_tags)
  if (!tags.length) return h('span', { class: 'muted' }, '—')
  const shown = tags.slice(0, 2)
  return h('div', { class: 'tag-cell' }, [
    ...shown.map(tag => h('span', { class: 'tag-chip' }, tag)),
    ...(tags.length > shown.length
      ? [h('span', { class: 'tag-more' }, `+${tags.length - shown.length}`)]
      : []),
  ])
}

function toTsv(list: Record<string, any>[]): string {
  const cols = visibleFields.value.filter(f => !f.hideInTable)
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
    : filteredRows.value
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
    const url = String(raw || '')
    if (!url) return '—'
    return h('span', { class: 'cell-url' }, [
      h('a', {
        class: 'cell-link',
        href: url,
        title: url,
        onClick: (e: MouseEvent) => { e.preventDefault(); openLink(url) },
      }, url),
      h('button', {
        class: 'cell-copy',
        type: 'button',
        title: '复制链接',
        'aria-label': '复制链接',
        onClick: async (e: MouseEvent) => {
          e.stopPropagation()
          try {
            await writeClipboard(url)
            message.success('已复制链接')
          } catch {
            message.error('复制失败')
          }
        },
      }, '复制'),
    ])
  }
  return cellValue(row, f)
}

/**
 * 行双击 → 打开编辑弹窗（与记事本列表的"单击即开"同一交互直觉）。
 * 操作列里的按钮 / 复制链接按钮上的双击不接管，避免一次双击弹出两层编辑。
 */
function rowProps(row: Record<string, any>) {
  return {
    // 行级样式（超期标黄等）由页面通过 rowClass 注入，组件本身不认识业务语义
    class: props.rowClass?.(row) || undefined,
    onDblclick: (event: MouseEvent) => {
      if ((event.target as HTMLElement | null)?.closest('button, a')) return
      openEdit(row)
    },
  }
}

/** 分页：0 行时整条隐藏（避免"空表还挂着 1 2 >"），有数据时可选每页条数 */
const paginationProps = computed(() => (filteredRows.value.length ? {
  pageSize: 10,
  showSizePicker: true,
  pageSizes: [10, 20, 50],
  prefix: ({ itemCount }: { itemCount: number | undefined }) => `共 ${itemCount ?? 0} 条`,
} : false))

const columns = computed<DataTableColumns>(() => {
  const cols: DataTableColumns = [{ type: 'selection' }]
  for (const f of visibleFields.value.filter(x => !x.hideInTable)) {
    cols.push({
      title: f.label,
      key: f.key,
      width: f.width,
      ellipsis: { tooltip: true },
      render: (row: any) => renderCell(f, row),
    })
  }
  cols.push({
    // 也固定在右侧：表格总宽超过容器时，放在中间会被固定操作列压住、看不见
    title: '标签', key: '__tags', width: 120, fixed: 'right',
    render: (row: any) => renderTags(row),
  })
  cols.push({
    title: '操作',
    key: '__actions',
    width: props.extra ? 230 : 160,
    fixed: 'right',
    render: (row: any) => h('div', {
      class: 'row',
      onDblclick: (event: MouseEvent) => event.stopPropagation(),
    }, [
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
      <span class="count-chip">{{ filteredRows.length }}</span>
      <div class="grow" />
      <n-button size="small" quaternary @click="load">刷新</n-button>
      <n-button size="small" quaternary :disabled="!checkedRowKeys.length" @click="copyRows">
        复制表格（TSV）
      </n-button>
      <n-button size="small" :disabled="!checkedRowKeys.length" type="error" quaternary
                @click="batchRemove">
        批量删除{{ checkedRowKeys.length ? ` (${checkedRowKeys.length})` : '' }}
      </n-button>
      <n-button size="small" type="primary" @click="openCreate">新增</n-button>
    </div>
    <biz-filter-bar
      v-if="filters?.length"
      :filters="filters" :values="filterValues" :options="filterOptions"
      :matched="filteredRows.length" :total="rows.length"
      @update="onFilterUpdate"
    >
      <template #views>
        <n-select
          v-if="views.length || activeView"
          size="small" clearable placeholder="视图" style="width: 150px"
          :value="activeView" :options="viewOptions" @update:value="applyView"
        />
        <n-button size="small" quaternary @click="openSaveView">存为视图</n-button>
        <n-button v-if="activeView" size="small" quaternary type="error" @click="removeView">
          删视图
        </n-button>
      </template>
    </biz-filter-bar>
    <div class="muted" style="margin-bottom: 10px">
      勾选行后可批量删除或按列复制；未勾选时「复制表格」导出当前筛选后的全部行（TSV，可直接粘贴到 Excel）。
    </div>
    <n-data-table
      v-model:checked-row-keys="checkedRowKeys"
      :row-key="(row: any) => row.id"
      :columns="columns" :data="filteredRows" :loading="loading"
      :row-props="rowProps"
      :bordered="false" size="small" :scroll-x="1344"
      :pagination="paginationProps"
    >
      <template #empty>
        <empty-state
          v-if="!loading"
          compact
          :title="filtersActive ? '没有匹配的记录' : `还没有${title}`"
          :desc="filtersActive
            ? '当前筛选条件下没有结果，试试放宽关键字或点「清除筛选」。'
            : (emptyDesc || '新建第一条后，它会自动进入全文索引，可在全局搜索里按关键词找到。')"
          :action-text="filtersActive ? '' : `新增${title}`"
          @action="openCreate"
        />
      </template>
    </n-data-table>
  </div>

  <n-modal
    v-model:show="showModal" preset="card"
    :title="(editingId ? '编辑' : '新增') + title"
    style="width: 780px; max-width: 94vw; max-height: 88vh; overflow: auto"
  >
    <div class="form-row">
      <label class="form-label">标签</label>
      <div class="grow">
        <n-dynamic-tags v-model:value="formTags" :max="10" />
      </div>
    </div>
    <div v-for="f in visibleFields" :key="f.key" class="form-row">
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

  <n-modal v-model:show="showViewModal" preset="card" title="存为视图" style="width: 380px">
    <n-input
      v-model:value="viewName" placeholder="例如：我的 BUG"
      maxlength="40" show-count @keyup.enter="commitView"
    />
    <div class="muted" style="margin-top: 8px">
      保存的是当前筛选条件，不是结果；下次打开会按当时的数据重算。
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showViewModal = false">取消</n-button>
        <n-button type="primary" @click="commitView">保存</n-button>
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
:deep(.cell-link:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
:deep(.cell-url) { display: inline-flex; align-items: center; gap: 6px; min-width: 0; max-width: 100%; }
:deep(.cell-url .cell-link) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
:deep(.cell-copy) {
  flex: 0 0 auto; padding: 0 5px; font-family: inherit; font-size: 12px; line-height: 18px;
  color: var(--ink-3); background: var(--paper-2); border: 1px solid transparent;
  border-radius: var(--r-xs); cursor: pointer; opacity: 0;
}
:deep(.n-data-table-tr:hover .cell-copy), :deep(.cell-copy:focus-visible) { opacity: 1; }
:deep(.tag-cell) { display: flex; gap: 4px; overflow: hidden; }
:deep(.tag-chip) {
  font-size: 11px; line-height: 16px; padding: 0 5px; white-space: nowrap;
  color: var(--ink-2); background: var(--paper-2); border-radius: var(--r-xs);
}
:deep(.tag-more) { font-size: 11px; color: var(--ink-3); }
</style>
