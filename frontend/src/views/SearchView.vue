<script setup lang="ts">
/**
 * 全局搜索（M2 + M3 + M6）：正文检索、按类型分组、命中片段、只读预览、
 * 「关联到项目」闭环、跨页跳转，以及语义相近 / 外部索引两个独立分组。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import {
  NAlert, NButton, NDrawer, NDrawerContent, NInput, NModal, NSelect,
  NSpin, NTooltip, useMessage,
} from 'naive-ui'
import { api, TOKEN } from '@/api/client'
import { useJumpStore } from '@/stores/jump'
import EmptyState from '@/components/EmptyState.vue'
import { useSearchStore, type SearchItem } from '@/stores/search'

type Item = SearchItem
interface Preview {
  title: string
  text: string
  tables: string[][][]
  images?: ImageMeta[]
  error: string
  source: string
  abs_path: string | null
  extract_status: string | null
}
/** 文档内嵌图片的元数据；index 与正文 [图 N] 的 N-1 对应 */
interface ImageMeta {
  index: number
  name: string
  kind: 'native' | 'vector'
  width: number
  height: number
  bytes: number
}
/** 正文片段：文字或图片锚点（img 为 0 基序号；block 表示该锚点独占一行） */
interface Seg { text?: string; img?: number; block?: boolean }
interface Project { id: number; name: string; entry_id: number | null }
interface Group {
  key: string
  label: string
  list: Item[]
  kind: 'literal' | 'semantic' | 'plugin'
  actions: boolean
}

interface SearchResult {
  query: string
  mode: string
  items: Item[]
  semantic_items?: Item[]
  plugin_items?: Item[]
}

const TYPE_LABEL: Record<string, string> = {
  file: '文件', project: '项目事项', worklog: '工作日志',
  incident: '运维记录', link: '企微链接',
}

/** 结果行操作图标（组合丙）：线性 1.5px，与 SideNav.vue 的 ICONS 同一套语言 */
const ICON_PATHS: Record<string, string[]> = {
  preview: [
    'M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6-10-6-10-6Z',
    'M12 9.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2Z',
  ],
  open: [
    'M14 4h6v6',
    'M20 4l-9 9',
    'M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5',
  ],
  folder: ['M3 6h6l2 2h10v10.5H3z'],
  note: [
    'M6 3h8l4 4v14H6z',
    'M14 3v4h4',
    'M9 12h6',
    'M9 16h4',
  ],
  incident: ['M12 4 3.5 19.5h17L12 4Z', 'M12 10v4.5', 'M12 17v.5'],
  link: [
    'M10 14 14 10',
    'M8.6 12.4 6.2 14.8a3.4 3.4 0 0 0 4.8 4.8l2.4-2.4',
    'M15.4 11.6 17.8 9.2a3.4 3.4 0 0 0-4.8-4.8L10.6 6.8',
  ],
}

const message = useMessage()
/** 抽取状态用中文：界面上不出现裸的 ok / failed */
const STATUS_LABEL: Record<string, string> = {
  ok: '正常', failed: '失败', pending: '待抽取', skipped: '已跳过', empty: '无正文',
}
function statusLabel(value: string | null | undefined): string {
  return value ? (STATUS_LABEL[value] ?? value) : '—'
}

const jump = useJumpStore()
const search = useSearchStore()
// 检索状态放在 store：页签切换会销毁本组件，状态不能留在组件内
const { q, items, semanticItems, pluginItems, mode, searched } = storeToRefs(search)
const busy = ref(false)

/** 标签过滤（2026-11-25）：与关键词可组合，也可单独使用 */
const activeTag = ref('')
const tagOptions = ref<{ tag: string; count: number }[]>([])

const showPreview = ref(false)
const preview = ref<Preview | null>(null)
const previewBusy = ref(false)

/* —— 预览阅读体验 ——
 * 抽屉宽度可拖拽 + 一键最大化；正文与表格都只渲染前若干行，
 * 宽表可单独「放大查看」到全屏，避免 640px 里塞进十几万单元格。 */
const DRAWER_DEFAULT_WIDTH = 760
const TEXT_PREVIEW_LINES = 80
const TABLE_PREVIEW_ROWS = 30
const BIG_TABLE_STEP = 200

const drawerWidth = ref(DRAWER_DEFAULT_WIDTH)
const previewMax = ref(false)
const viewportWidth = ref(window.innerWidth)
const maxDrawerWidth = computed(() => Math.max(560, Math.round(viewportWidth.value * 0.96)))

const textExpanded = ref(false)
const expandedTables = ref<number[]>([])

const showBigTable = ref(false)
const bigTableIndex = ref(0)
const bigRows = ref(BIG_TABLE_STEP)

/** 预览结果按 entry 缓存在内存：同一文件二次打开不再走 2 秒抽取 */
const previewCache = new Map<number, Preview>()
const lastEntryId = ref<number | null>(null)

const textLines = computed(() => (preview.value?.text ?? '').split('\n'))
const shownText = computed(() =>
  textExpanded.value ? textLines.value : textLines.value.slice(0, TEXT_PREVIEW_LINES),
)
const bigTable = computed<string[][]>(() => preview.value?.tables[bigTableIndex.value] ?? [])
const bigBody = computed(() => bigTable.value.slice(1, bigRows.value + 1))

/* —— 内嵌图片 ——
 * 后端只发元数据，字节按需走 /api/preview/image（缩略图 320、原图 1600 封顶）。
 * 正文里的 [图 N] 锚点按原位渲染成图：独占一行出块级大图，夹在文字中出行内小图；
 * 底部网格降级为折叠区（默认收起），只在需要按序号翻阅时展开。 */
const IMAGE_PREVIEW_MAX = 60
const imgAll = ref(false)
const imgOpen = ref(false)
const showViewer = ref(false)
const viewerIndex = ref(0)
const viewerFit = ref(true)
/** 加载失败的图退回「图 N」徽标，避免留白框 */
const imgFailed = ref<Set<number>>(new Set())

const allImages = computed<ImageMeta[]>(() => preview.value?.images ?? [])
const shownImages = computed<ImageMeta[]>(() =>
  imgAll.value ? allImages.value : allImages.value.slice(0, IMAGE_PREVIEW_MAX),
)
const viewerImage = computed<ImageMeta | null>(() => allImages.value[viewerIndex.value] ?? null)
const imgByIndex = computed(() => new Map(allImages.value.map(m => [m.index, m])))

function imgMeta(i: number): ImageMeta | undefined {
  return imgByIndex.value.get(i)
}

function imgError(i: number) {
  if (!imgFailed.value.has(i)) imgFailed.value.add(i)
}

function imgUrl(i: number, thumb: boolean) {
  const base = `/api/preview/image?entry_id=${lastEntryId.value ?? 0}&i=${i}&thumb=${thumb ? 1 : 0}`
  return TOKEN ? `${base}&token=${encodeURIComponent(TOKEN)}` : base
}

function openViewer(i: number) {
  viewerIndex.value = i
  viewerFit.value = true
  showViewer.value = true
}

function stepViewer(delta: number) {
  const n = allImages.value.length
  if (!n) return
  viewerIndex.value = (viewerIndex.value + delta + n) % n
  viewerFit.value = true
}

const textSegments = computed<Seg[]>(() => segments(shownText.value.join('\n')))

/**
 * 把带 [图 N] 的文本切成「文字 / 图片锚点」片段。
 * 锚点所在行去掉标记后只剩空白 → 判为块级（图片原本独占一段），否则按行内小图。
 */
function segments(text: string): Seg[] {
  if (!text || !text.includes('[图 ')) return [{ text: text ?? '' }]
  const out: Seg[] = []
  const re = /\[图 (\d+)\]/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) })
    const lineStart = text.lastIndexOf('\n', m.index - 1) + 1
    const nlAfter = text.indexOf('\n', m.index + m[0].length)
    const lineEnd = nlAfter === -1 ? text.length : nlAfter
    const block = text.slice(lineStart, m.index).trim() === ''
      && text.slice(m.index + m[0].length, lineEnd).trim() === ''
    out.push({ img: Number(m[1]) - 1, block })
    last = m.index + m[0].length
  }
  if (last < text.length) out.push({ text: text.slice(last) })
  return out
}

function onViewportResize() {
  viewportWidth.value = window.innerWidth
  if (previewMax.value) drawerWidth.value = maxDrawerWidth.value
}

function togglePreviewMax() {
  previewMax.value = !previewMax.value
  drawerWidth.value = previewMax.value ? maxDrawerWidth.value : DRAWER_DEFAULT_WIDTH
}

function isTableExpanded(i: number) {
  return expandedTables.value.includes(i)
}

function toggleTable(i: number) {
  expandedTables.value = isTableExpanded(i)
    ? expandedTables.value.filter(x => x !== i)
    : [...expandedTables.value, i]
}

/** 首行当表头；展开时不截断 */
function tableBody(t: string[][], i: number) {
  const rows = isTableExpanded(i) ? t : t.slice(0, TABLE_PREVIEW_ROWS + 1)
  return rows.slice(1)
}

function maxCols(t: string[][] | undefined) {
  return (t ?? []).reduce((n, row) => Math.max(n, row.length), 0)
}

function openBigTable(i: number) {
  bigTableIndex.value = i
  bigRows.value = BIG_TABLE_STEP
  showBigTable.value = true
}

function loadMoreRows() {
  bigRows.value += BIG_TABLE_STEP
}

async function copyTableTsv(t: string[][]) {
  const tsv = t.map(row => row.join('\t')).join('\n')
  try {
    await navigator.clipboard.writeText(tsv)
    message.success(`已复制 ${t.length} 行，可直接粘贴到 Excel`)
  } catch (e) {
    message.error('复制失败：' + String(e))
  }
}

const showLink = ref(false)
const linking = ref(false)
const linkTarget = ref<Item | null>(null)
const linkProjectId = ref<number | null>(null)
const projects = ref<Project[]>([])
const projectOptions = computed(() =>
  projects.value
    .filter(p => p.entry_id != null)
    .map(p => ({ label: p.name, value: p.id })),
)

const groups = computed(() => {
  const m = new Map<string, Item[]>()
  for (const it of items.value) {
    const key = it.type || 'file'
    if (!m.has(key)) m.set(key, [])
    m.get(key)!.push(it)
  }
  return Array.from(m.entries()).map(([key, list]) => ({
    key, label: TYPE_LABEL[key] ?? key, list,
  }))
})

const allGroups = computed<Group[]>(() => {
  const out: Group[] = groups.value.map(g => ({
    key: g.key, label: g.label, list: g.list, kind: 'literal', actions: true,
  }))
  if (semanticItems.value.length) {
    out.push({ key: 'semantic', label: '语义相近', list: semanticItems.value,
               kind: 'semantic', actions: true })
  }
  if (pluginItems.value.length) {
    out.push({ key: 'plugin', label: '外部索引', list: pluginItems.value,
               kind: 'plugin', actions: false })
  }
  return out
})

async function run() {
  const keyword = q.value.trim()
  if (!keyword && !activeTag.value) {
    search.clear()
    return
  }
  busy.value = true
  searched.value = true
  try {
    const params = new URLSearchParams()
    if (keyword) params.set('q', keyword)
    if (activeTag.value) params.set('tag', activeTag.value)
    const r = await api<SearchResult>('/api/search?' + params.toString())
    search.setResult(r)
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

async function loadTags() {
  try {
    tagOptions.value = await api<{ tag: string; count: number }[]>('/api/tags')
  } catch {
    // 标签读不出来不影响检索
  }
}

function pickTag(tag: string) {
  activeTag.value = tag
  if (!tag && !q.value.trim()) {
    search.clear()
    return
  }
  run()
}

async function openByPath(path: string | null, reveal: boolean) {
  if (!path) return
  try {
    await api('/api/open', {
      method: 'POST',
      body: JSON.stringify({ path, reveal }),
    })
    message.success(reveal ? '已在资源管理器中定位' : '已交回本机程序打开')
  } catch (e) {
    message.error(String(e))
  }
}

function openFile(item: Item, reveal: boolean) {
  return openByPath(item.abs_path, reveal)
}

function openExternal(url: string | undefined) {
  if (!url) return
  if (!/^https?:\/\//i.test(url)) {
    message.error('只允许打开 http/https 链接')
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}

const PREVIEW_CACHE_MAX = 3

async function openPreviewById(entryId: number) {
  showPreview.value = true
  // 换条目时回到「折叠 + 默认宽度」，避免上一个文件的展开状态串到新文件
  if (lastEntryId.value !== entryId) {
    lastEntryId.value = entryId
    textExpanded.value = false
    expandedTables.value = []
    imgAll.value = false
    imgOpen.value = false
    imgFailed.value = new Set()
    if (previewMax.value) {
      previewMax.value = false
      drawerWidth.value = DRAWER_DEFAULT_WIDTH
    }
  }
  const cached = previewCache.get(entryId)
  if (cached) {
    previewBusy.value = false
    preview.value = cached
    return
  }
  previewBusy.value = true
  preview.value = null
  try {
    const data = await api<Preview>('/api/preview?entry_id=' + entryId)
    preview.value = data
    previewCache.set(entryId, data)
    while (previewCache.size > PREVIEW_CACHE_MAX) {
      const oldest = previewCache.keys().next().value
      if (oldest === undefined) break
      previewCache.delete(oldest)
    }
  } catch (e) {
    message.error(String(e))
  } finally {
    previewBusy.value = false
  }
}

function openPreview(item: Item) {
  return openPreviewById(item.id)
}

async function loadProjects() {
  try {
    projects.value = await api<Project[]>('/api/biz/projects')
  } catch (e) {
    message.error(String(e))
  }
}

function openLinkDialog(item: Item) {
  linkTarget.value = item
  linkProjectId.value = null
  showLink.value = true
  loadProjects()
}

async function submitLink() {
  if (!linkTarget.value || !linkProjectId.value) return
  linking.value = true
  try {
    await api(`/api/entries/${linkTarget.value.id}/link-project`, {
      method: 'POST',
      body: JSON.stringify({ project_id: linkProjectId.value }),
    })
    message.success('已关联到项目')
    showLink.value = false
  } catch (e) {
    message.error(String(e))
  } finally {
    linking.value = false
  }
}

function sourceLabel(it: Item) {
  if (it.type === 'plugin') return it.source || '外部索引'
  if (it.type === 'link') return '企微链接'
  if (it.type === 'note') return '记事本'
  if (it.type === 'incident') return '运维记录'
  if (it.type !== 'file') return '业务记录'
  return it.root_kind === 'anyshare_sync' ? '爱数同步盘' : '本地'
}

/** 记事本条目：跳到记事本页签并直接打开对应页面（jump store 由 NotesView 消费） */
function openInNotes(it: Item) {
  const noteId = Number(it.ref_id || 0)
  if (!noteId) { message.warning('这条记录还没有对应的记事本页面'); return }
  jump.jumpToNote(noteId)
}

/** 运维记录条目：跳回运维记录页签并直接打开该条的编辑弹窗（jump store 由 IncidentsView 消费） */
function openInIncidents(it: Item) {
  const incidentId = Number(it.ref_id || 0)
  if (!incidentId) { message.warning('这条记录没有对应的运维记录明细'); return }
  jump.jumpToIncident(incidentId)
}

function groupEyebrow(g: Group) {
  if (g.kind === 'semantic') return '语义扩展'
  if (g.kind === 'plugin') return '外部来源'
  return '字面命中'
}

function escapeHtml(s: string) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** 命中词高亮：先转义正文再包裹 <mark>，避免文件内容被当作 HTML 执行 */
function highlight(text: string) {
  const safe = escapeHtml(text)
  const raw = q.value.trim()
  if (!raw) return safe
  const terms = raw.split(/\s+/).filter(Boolean).map(t => escapeRe(escapeHtml(t)))
  if (!terms.length) return safe
  try {
    return safe.replace(new RegExp('(' + terms.join('|') + ')', 'gi'), '<mark>$1</mark>')
  } catch {
    return safe
  }
}

function focusSearch() {
  window.requestAnimationFrame(() => {
    const el = document.querySelector<HTMLInputElement>('.onlyone-search input')
    el?.focus()
    el?.select()
  })
}

function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    focusSearch()
  }
}

// 关键词变化即落盘；结果含正文片段，只保留在内存中
watch(q, () => search.persistQuery())

watch(() => jump.pendingEntryId, (val) => {
  if (!val) return
  const id = jump.consumeEntryId()
  if (id) openPreviewById(id)
}, { immediate: true })

watch(() => jump.pendingQuery, (val) => {
  if (!val) return
  const query = jump.consumeQuery()
  if (query) {
    // 外部跳转带来的是明确的关键词，清掉上一次的标签过滤，避免两个条件叠加出空结果
    activeTag.value = ''
    q.value = query
    run()
  }
}, { immediate: true })

/** 空状态主操作：只有「搜过但没结果」时才渲染按钮 —— 清空关键词回到初始态。
 *  没有索引目录的情况交给首启向导处理，这里不再放「去添加索引目录」，避免误导。 */
function onEmptyAction() {
  q.value = ''
  items.value = []
  semanticItems.value = []
  pluginItems.value = []
  searched.value = false
  focusSearch()
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('onlyone:focus-search', focusSearch)
  window.addEventListener('resize', onViewportResize)
  loadProjects()
  loadTags()
  // 跨重启只留关键词：有词且本次会话尚未检索过时，自动重跑一次。
  // URL 深链接 ?q= 走 pendingQuery 的 immediate watch，会先把 searched 置真，避免重复请求。
  if (q.value.trim() && !searched.value) void run()
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('onlyone:focus-search', focusSearch)
  window.removeEventListener('resize', onViewportResize)
})
</script>

<template>
  <div class="card">
    <div class="row">
      <n-input
        v-model:value="q" class="onlyone-search grow"
        placeholder="搜索文件正文、标题或业务记录；Ctrl+K 聚焦" @keyup.enter="run"
      />
      <n-button type="primary" :loading="busy" @click="run">搜索</n-button>
    </div>
    <div v-if="tagOptions.length" class="tag-bar">
      <span class="tag-bar-label">标签</span>
      <span class="tag-pill" :class="{ active: !activeTag }" @click="pickTag('')">全部</span>
      <span v-for="t in tagOptions.slice(0, 12)" :key="t.tag" class="tag-pill"
            :class="{ active: activeTag === t.tag }" @click="pickTag(t.tag)">
        {{ t.tag }}<span class="tag-pill-count">{{ t.count }}</span>
      </span>
      <span v-if="activeTag" class="tag-bar-clear" @click="pickTag('')">清除标签</span>
    </div>
    <div v-if="searched" class="result-bar">
      <span class="metric"><b>{{ items.length }}</b> 字面命中</span>
      <span class="metric"><b>{{ semanticItems.length }}</b> 语义相近</span>
      <span class="metric"><b>{{ pluginItems.length }}</b> 外部索引</span>
      <span class="mode">{{ mode === 'fts5_trigram' ? 'FTS5 trigram' : 'LIKE 兜底（短词）' }}</span>
    </div>
  </div>

  <div v-if="!allGroups.length" class="card" style="margin-top: 14px">
    <empty-state
      :title="searched ? `没找到与「${q.trim()}」匹配的内容` : '输入关键词开始搜索'"
      :desc="searched
        ? '试试更短的词或只保留关键词；两个字以内的短词会自动退回字面匹配，命中率更高。'
        : '可搜索文件正文与标题，以及项目、运维记录、企微链接与记事本；多个词用空格分隔即可。'"
      :action-text="searched ? '清空关键词' : ''"
      :hint="searched ? '或按 Ctrl+K 重新聚焦搜索框' : '按 Ctrl+K 可随时聚焦搜索框'"
      @action="onEmptyAction"
    />
  </div>

  <div v-for="g in allGroups" :key="g.key" class="card" style="margin-top: 14px">
    <h3 class="group-title">
      <span class="eyebrow">{{ groupEyebrow(g) }}</span>
      <span class="group-label">{{ g.label }}</span>
      <span class="count-chip">{{ g.list.length }}</span>
      <span v-if="g.kind === 'plugin'" class="muted">只读外部索引，不参与关联</span>
    </h3>
    <div v-for="it in g.list" :key="it.id" class="hit">
      <div class="hit-head">
        <span class="hit-badge" :class="{ 'is-title': it.hit_in_title }">
          {{ it.hit_in_title ? '标题命中'
             : (g.kind === 'semantic' ? '语义' : (g.kind === 'plugin' ? '外部' : '正文命中')) }}
        </span>
        <span class="hit-title" :title="it.title">{{ it.title }}</span>
        <span class="hit-source">
          {{ g.kind === 'semantic' ? '相似度 ' + (it.score ?? 0).toFixed(2) : sourceLabel(it) }}
        </span>
      </div>
      <div v-if="it.snippet" class="snippet-row">
        <div class="snippet" v-html="highlight(it.snippet)" />
        <span v-if="it.hit_count" class="hit-count">共 {{ it.hit_count }} 处</span>
      </div>
      <div class="hit-foot">
        <span class="path" :title="it.abs_path ?? (it.url || '—')">
          {{ it.abs_path ?? (it.url || '—') }}
        </span>
        <div v-if="g.actions" class="actions">
          <n-tooltip v-if="it.type === 'note'" trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="在记事本中打开"
                      @click="openInNotes(it)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.note" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            在记事本中打开
          </n-tooltip>
          <n-tooltip v-if="it.type === 'incident'" trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="在运维记录中打开"
                      @click="openInIncidents(it)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.incident" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            在运维记录中打开
          </n-tooltip>
          <n-tooltip trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="只读预览" @click="openPreview(it)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.preview" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            只读预览
          </n-tooltip>
          <n-tooltip trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="用本机程序打开"
                      :disabled="!it.abs_path" @click="openFile(it, false)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.open" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            用本机程序打开
          </n-tooltip>
          <n-tooltip trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="在资源管理器中显示"
                      :disabled="!it.abs_path" @click="openFile(it, true)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.folder" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            在资源管理器中显示
          </n-tooltip>
          <n-tooltip trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="关联到项目"
                      @click="openLinkDialog(it)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.link" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            关联到项目
          </n-tooltip>
        </div>
        <div v-else-if="it.url" class="actions">
          <n-tooltip trigger="hover">
            <template #trigger>
              <button class="icon-btn" type="button" aria-label="用默认浏览器打开"
                      @click="openExternal(it.url)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
                     stroke-linecap="round" stroke-linejoin="round">
                  <path v-for="d in ICON_PATHS.open" :key="d" :d="d" />
                </svg>
              </button>
            </template>
            用默认浏览器打开
          </n-tooltip>
        </div>
      </div>
    </div>
  </div>

  <n-drawer
    v-model:show="showPreview" v-model:width="drawerWidth" placement="right"
    resizable :default-width="DRAWER_DEFAULT_WIDTH" :min-width="520" :max-width="maxDrawerWidth"
  >
    <n-drawer-content :native-scrollbar="false" closable>
      <template #header>
        <div class="pv-head">
          <span class="pv-head-title" :title="preview?.title">
            {{ preview?.title ?? '只读预览' }}
          </span>
          <n-button size="tiny" quaternary @click="togglePreviewMax">
            {{ previewMax ? '还原宽度' : '最大化' }}
          </n-button>
        </div>
      </template>
      <n-spin v-if="previewBusy" size="small" />
      <template v-else-if="preview">
        <div class="muted" style="margin-bottom: 10px">
          来源：{{ preview.source }} · 抽取状态：{{ statusLabel(preview.extract_status) }}
        </div>
        <n-alert v-if="preview.error" type="warning" style="margin-bottom: 12px">
          正文抽取失败：{{ preview.error }}
        </n-alert>

        <template v-if="preview.text">
          <div class="pv-text"><template v-for="(s, si) in textSegments" :key="si">
            <span v-if="s.img === undefined">{{ s.text }}</span>
            <figure v-else-if="s.block" class="pv-fig" :title="`打开第 ${s.img + 1} 张图`"
                    @click="openViewer(s.img)">
              <img v-if="!imgFailed.has(s.img)" :src="imgUrl(s.img, false)" loading="lazy"
                   decoding="async" :alt="`图 ${s.img + 1}`" @error="imgError(s.img)" />
              <button v-else class="pv-chip" @click.stop="openViewer(s.img)">
                图 {{ s.img + 1 }}
              </button>
              <figcaption>
                图 {{ s.img + 1 }}<template v-if="imgMeta(s.img)"> · {{ imgMeta(s.img)!.width }} ×
                {{ imgMeta(s.img)!.height }}</template>
              </figcaption>
            </figure>
            <template v-else>
              <img v-if="!imgFailed.has(s.img)" class="pv-inline-img" :src="imgUrl(s.img, true)"
                   loading="lazy" decoding="async" :alt="`图 ${s.img + 1}`"
                   :title="`打开第 ${s.img + 1} 张图`"
                   @click="openViewer(s.img)" @error="imgError(s.img)" />
              <button v-else class="pv-chip" @click="openViewer(s.img)">图 {{ s.img + 1 }}</button>
            </template>
          </template></div>
          <div v-if="textLines.length > TEXT_PREVIEW_LINES" class="pv-more">
            <n-button size="tiny" quaternary @click="textExpanded = !textExpanded">
              {{ textExpanded ? '收起正文' : `展开全文（共 ${textLines.length} 行）` }}
            </n-button>
          </div>
        </template>
        <div v-else class="muted">（无正文）</div>

        <div v-for="(t, ti) in preview.tables" :key="ti" class="pv-wrap">
          <div class="pv-table-head">
            <span class="muted">表格 {{ ti + 1 }} · {{ t.length }} 行 × {{ maxCols(t) }} 列</span>
            <div class="grow" />
            <n-button v-if="t.length > TABLE_PREVIEW_ROWS + 1" size="tiny" quaternary
                      @click="toggleTable(ti)">
              {{ isTableExpanded(ti) ? '收起' : `展开全部（${t.length} 行）` }}
            </n-button>
            <n-button size="tiny" quaternary @click="openBigTable(ti)">放大查看</n-button>
            <n-button size="tiny" quaternary @click="copyTableTsv(t)">复制整表</n-button>
          </div>
          <div class="pv-table-scroll">
            <table class="pv-table">
              <thead>
                <tr><td v-for="(cell, ci) in t[0]" :key="ci"><template
                  v-for="(s, si) in segments(cell)" :key="si"
                ><span v-if="s.img === undefined">{{ s.text }}</span><template v-else><img
                  v-if="!imgFailed.has(s.img)" class="pv-cell-img" :src="imgUrl(s.img, true)"
                  loading="lazy" decoding="async" :alt="`图 ${s.img + 1}`"
                  @click="openViewer(s.img)" @error="imgError(s.img)"
                /><button v-else class="pv-chip" @click="openViewer(s.img)"
                >图 {{ s.img + 1 }}</button></template></template></td></tr>
              </thead>
              <tbody>
                <tr v-for="(row, ri) in tableBody(t, ti)" :key="ri">
                  <td v-for="(cell, ci) in row" :key="ci"><template
                    v-for="(s, si) in segments(cell)" :key="si"
                  ><span v-if="s.img === undefined">{{ s.text }}</span><template v-else><img
                    v-if="!imgFailed.has(s.img)" class="pv-cell-img" :src="imgUrl(s.img, true)"
                    loading="lazy" decoding="async" :alt="`图 ${s.img + 1}`"
                    @click="openViewer(s.img)" @error="imgError(s.img)"
                  /><button v-else class="pv-chip" @click="openViewer(s.img)"
                  >图 {{ s.img + 1 }}</button></template></template></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="!isTableExpanded(ti) && t.length > TABLE_PREVIEW_ROWS + 1"
               class="muted pv-hint">
            仅渲染前 {{ TABLE_PREVIEW_ROWS }} 行，点「展开全部」或「放大查看」看余下内容
          </div>
        </div>

        <div v-if="allImages.length" class="pv-images">
          <button class="pv-img-toggle" type="button" @click="imgOpen = !imgOpen">
            <span class="pv-caret">{{ imgOpen ? '▾' : '▸' }}</span>
            <span>全部图片 {{ allImages.length }} 张</span>
            <span class="pv-img-toggle-hint">正文已按原位显示，展开可按序号翻阅</span>
          </button>
          <template v-if="imgOpen">
            <div class="pv-table-head">
              <span class="muted">缩略图</span>
              <div class="grow" />
              <n-button v-if="allImages.length > IMAGE_PREVIEW_MAX && !imgAll" size="tiny" quaternary
                        @click="imgAll = true">
                显示全部（{{ allImages.length }} 张）
              </n-button>
              <n-button v-else-if="imgAll && allImages.length > IMAGE_PREVIEW_MAX" size="tiny" quaternary
                        @click="imgAll = false">
                只看前 {{ IMAGE_PREVIEW_MAX }} 张
              </n-button>
            </div>
            <div class="pv-grid">
              <button v-for="im in shownImages" :key="im.index" class="pv-thumb"
                      :title="`${im.name} · ${im.width}×${im.height}`"
                      @click="openViewer(im.index)">
                <img :src="imgUrl(im.index, true)" loading="lazy" :alt="`图 ${im.index + 1}`" />
                <span class="pv-thumb-cap">图 {{ im.index + 1 }} · {{ im.width }}×{{ im.height }}</span>
              </button>
            </div>
            <div v-if="!imgAll && allImages.length > IMAGE_PREVIEW_MAX" class="muted pv-hint">
              仅渲染前 {{ IMAGE_PREVIEW_MAX }} 张缩略图，点「显示全部」看余下
              {{ allImages.length - IMAGE_PREVIEW_MAX }} 张
            </div>
          </template>
        </div>

        <div class="row" style="margin-top: 16px">
          <n-button v-if="preview.abs_path" size="small" secondary
                    @click="openByPath(preview.abs_path, false)">
            用本机程序打开
          </n-button>
          <n-button v-if="preview.abs_path" size="small" quaternary
                    @click="openByPath(preview.abs_path, true)">
            在资源管理器中显示
          </n-button>
        </div>
      </template>
      <div v-else class="muted">没有可显示的内容。</div>
    </n-drawer-content>
  </n-drawer>

  <n-modal
    v-model:show="showBigTable" preset="card"
    :title="`表格 ${bigTableIndex + 1} · ${bigTable.length} 行 × ${maxCols(bigTable)} 列`"
    :style="{ width: '92vw', maxWidth: '1600px' }"
  >
    <div class="pv-big-scroll">
      <table class="pv-table is-big">
        <thead>
          <tr><td v-for="(cell, ci) in (bigTable[0] ?? [])" :key="ci">{{ cell }}</td></tr>
        </thead>
        <tbody>
          <tr v-for="(row, ri) in bigBody" :key="ri">
            <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <template #footer>
      <div class="row" style="width: 100%; justify-content: flex-end; gap: 10px">
        <span class="muted">
          已显示 {{ Math.min(bigRows, Math.max(0, bigTable.length - 1)) }} /
          {{ Math.max(0, bigTable.length - 1) }} 行
        </span>
        <n-button v-if="bigRows < bigTable.length - 1" size="small" @click="loadMoreRows">
          再加载 {{ BIG_TABLE_STEP }} 行
        </n-button>
        <n-button size="small" @click="copyTableTsv(bigTable)">复制整表 TSV</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal
    v-model:show="showViewer" preset="card"
    :title="viewerImage ? `图 ${viewerIndex + 1} / ${allImages.length}` : '图片'"
    :style="{ width: '94vw', maxWidth: '1800px' }"
  >
    <div class="muted" style="margin-bottom: 8px">
      {{ viewerImage?.name }} · {{ viewerImage?.width }}×{{ viewerImage?.height }}
      <template v-if="viewerImage?.kind === 'vector'">· 矢量图（已栅格化）</template>
    </div>
    <div class="pv-viewer" :class="{ 'is-fit': viewerFit }">
      <img v-if="viewerImage" :src="imgUrl(viewerIndex, false)" :alt="`图 ${viewerIndex + 1}`"
           :title="viewerFit ? '点击查看原始尺寸' : '点击适应窗口'"
           @click="viewerFit = !viewerFit" />
    </div>
    <template #footer>
      <div class="row" style="width: 100%; gap: 10px">
        <n-button size="small" :disabled="allImages.length < 2" @click="stepViewer(-1)">上一张</n-button>
        <n-button size="small" :disabled="allImages.length < 2" @click="stepViewer(1)">下一张</n-button>
        <div class="grow" />
        <n-button size="small" quaternary @click="viewerFit = !viewerFit">
          {{ viewerFit ? '原始尺寸' : '适应窗口' }}
        </n-button>
        <n-button size="small" quaternary @click="showViewer = false">关闭</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showLink" preset="card" title="关联到项目" style="width: 420px">
    <div class="muted" style="margin-bottom: 10px">
      把「{{ linkTarget?.title }}」关联到项目
    </div>
    <n-select v-model:value="linkProjectId" :options="projectOptions" placeholder="选择项目" />
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showLink = false">取消</n-button>
        <n-button type="primary" :disabled="!linkProjectId" :loading="linking" @click="submitLink">
          关联
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.tag-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  margin-top: 10px; padding: 6px 2px;
}
.tag-bar-label { font-size: 11px; color: var(--ink-3); margin-right: 2px; }
.tag-pill {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; line-height: 20px; padding: 0 8px;
  color: var(--ink-2); background: var(--paper-2);
  border: 1px solid transparent; border-radius: 999px; cursor: pointer;
}
.tag-pill:hover { border-color: var(--line); }
.tag-pill.active { color: var(--accent-strong); background: var(--accent-soft); }
.tag-pill-count { font-size: 11px; color: var(--ink-3); }
.tag-pill.active .tag-pill-count { color: var(--accent-strong); }
.tag-bar-clear { font-size: 11px; color: var(--info-deep); cursor: pointer; text-decoration: underline; }
.result-bar {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line-soft);
  font-size: 12px; color: var(--ink-3);
}
.result-bar .metric b { font-size: 14px; font-weight: 500; color: var(--ink); margin-right: 3px; }
.result-bar .mode {
  margin-left: auto; font-size: 12px; letter-spacing: 0.04em;
  color: var(--info-deep); background: var(--info-soft);
  border-radius: 4px; padding: 2px 7px;
}
.group-title { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
.group-label { font-size: 14px; font-weight: 500; color: var(--ink); }
.hit { padding: 12px 0; border-top: 1px solid var(--line-soft); }
.hit:first-of-type { border-top: none; }
.hit-head { display: flex; align-items: center; gap: 8px; }
.hit-badge {
  flex: 0 0 auto; font-size: 12px; line-height: 18px; padding: 0 7px;
  border-radius: 4px; background: var(--paper-2); color: var(--ink-2);
}
.hit-badge.is-title { background: var(--info-soft); color: var(--info-deep); }
.hit-title {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 14px; font-weight: 500; color: var(--ink);
  background: transparent; padding: 4px 0;
}
.hit-source { flex: 0 0 auto; font-size: 12px; color: var(--ink-3); }
.snippet-row { display: flex; align-items: flex-start; gap: 8px; margin-top: 7px; }
.snippet {
  flex: 1; min-width: 0; border-left: 2px solid var(--line); padding-left: 10px;
  font-size: 12px; color: var(--ink-2); line-height: 1.65;
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; word-break: break-word;
}
.snippet :deep(mark) { font-weight: 500; }
.hit-count { flex: 0 0 auto; font-size: 12px; color: var(--ink-3); padding-top: 2px; }
.hit-foot { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.path {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 12px; color: var(--ink-3);
}
.actions { flex: 0 0 auto; display: flex; gap: 5px; }
.icon-btn {
  width: 28px; height: 28px; flex: 0 0 auto; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--line-soft); border-radius: var(--radius-sm);
  background: var(--surface); color: var(--ink-2); cursor: pointer;
}
.icon-btn:hover:not(:disabled) { border-color: var(--line); background: var(--paper-2); color: var(--ink); }
.icon-btn:disabled { opacity: 0.38; cursor: not-allowed; }
.icon-btn svg { width: 16px; height: 16px; }
.pv-text {
  white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.7;
  font-family: inherit; margin: 0; padding: 12px; background: var(--paper-2);
  border-radius: var(--radius-sm);
}
.pv-head { display: flex; align-items: center; gap: 10px; width: 100%; }
.pv-head-title {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pv-more { margin-top: 6px; }
.pv-table-head { display: flex; align-items: center; gap: 4px; margin: 14px 0 6px; flex-wrap: wrap; }
.pv-table-scroll {
  overflow: auto; max-height: min(62vh, 760px);
  border: 1px solid var(--line-soft); border-radius: var(--radius-sm);
}
/* 表格铺满抽屉宽度：原来按内容撑开，宽抽屉右侧会留一大片空白 */
.pv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.pv-table td {
  border: 1px solid var(--line); padding: 5px 9px; vertical-align: top;
  min-width: 64px; max-width: 320px; word-break: break-word;
}
.pv-table tbody tr:hover td { background: var(--paper); }
.pv-table thead td {
  position: sticky; top: 0; z-index: 1;
  background: var(--paper-3); font-weight: 500; color: var(--ink); white-space: nowrap;
}
.pv-hint { margin-top: 4px; font-size: 12px; }
.pv-chip {
  display: inline-block; margin: 0 2px; padding: 1px 6px; font-size: 12px;
  font-family: inherit; color: var(--info-deep); background: var(--info-soft);
  border: none; border-radius: 4px; cursor: pointer; vertical-align: baseline;
}
.pv-chip:hover { filter: brightness(0.95); }
.pv-fig {
  margin: 6px auto; padding: 0; max-width: 420px; cursor: zoom-in;
}
.pv-fig img {
  display: block; margin: 0 auto; width: auto; height: auto;
  max-width: 100%; max-height: 340px;
  background: var(--surface); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
}
.pv-fig figcaption {
  margin-top: 4px; font-size: 12px; color: var(--ink-3);
  text-align: center; white-space: normal;
}
.pv-inline-img {
  display: inline-block; vertical-align: middle; margin: 0 4px;
  width: auto; height: auto; max-width: 180px; max-height: 130px;
  background: var(--surface); border: 1px solid var(--line-soft);
  border-radius: 4px; cursor: zoom-in;
}
.pv-cell-img {
  display: block; margin: 2px 0; width: auto; height: auto;
  max-width: 160px; max-height: 120px;
  background: var(--surface); border: 1px solid var(--line-soft);
  border-radius: 4px; cursor: zoom-in;
}
.pv-img-toggle {
  display: flex; align-items: center; gap: 6px; width: 100%; margin: 14px 0 6px;
  padding: 8px 10px; font-family: inherit; font-size: 12px; text-align: left;
  color: var(--ink); background: var(--paper); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm); cursor: pointer;
}
.pv-img-toggle:hover { border-color: var(--line); }
.pv-caret { font-size: 12px; color: var(--ink-3); }
.pv-img-toggle-hint { font-size: 12px; color: var(--ink-3); }
.pv-images { margin-top: 16px; }
.pv-grid {
  display: grid; gap: 8px;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
}
.pv-thumb {
  display: flex; flex-direction: column; gap: 4px; padding: 6px; cursor: pointer;
  background: var(--paper-2); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm); text-align: left;
}
.pv-thumb:hover { border-color: var(--line); }
.pv-thumb img {
  width: 100%; height: 96px; object-fit: contain;
  background: #fff; border-radius: 3px;
}
.pv-thumb-cap {
  font-size: 12px; color: var(--ink-3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pv-viewer {
  display: flex; align-items: center; justify-content: center;
  max-height: calc(100vh - 260px); overflow: auto;
  background: var(--paper-2); border-radius: var(--radius-sm);
}
.pv-viewer.is-fit img { max-width: 100%; max-height: calc(100vh - 280px); cursor: zoom-in; }
.pv-viewer:not(.is-fit) { align-items: flex-start; justify-content: flex-start; }
.pv-viewer:not(.is-fit) img { max-width: none; max-height: none; cursor: zoom-out; }
.pv-big-scroll { max-height: calc(100vh - 250px); overflow: auto; }
.pv-table.is-big td { min-width: 96px; max-width: 480px; }
</style>
