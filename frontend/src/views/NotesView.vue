<script setup lang="ts">
/**
 * 记事本（M7，B 档）：笔记本 → 分区 → 页面三级树 + TipTap 富文本 +
 * 防抖自动保存（revision 乐观锁）+ 图片粘贴 / 压缩 / 转存 + 私密页 + 导出/全库备份。
 *
 * 图片链路：截图 Ctrl+V 或工具栏选文件 → 超过 5 MiB 前端压缩 → base64 上传
 * → 后端按内容哈希落盘（同页去重）→ 插入 /api/attachments/ 本地 URL。
 * 图文混排粘贴时由 transformPastedHTML 剥离外链 / data: 图片再异步转存，
 * CORS 失败降级忽略并提示；文字始终交给 TipTap 默认处理，不丢失。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorContent, useEditor } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import TiptapImage from '@tiptap/extension-image'
import { Table, TableCell, TableHeader, TableRow } from '@tiptap/extension-table'
import {
  NButton, NDropdown, NInput, NInputNumber, NModal, NSelect, NSwitch, NTag,
  useDialog, useMessage,
} from 'naive-ui'
import { api, TOKEN } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import { useJumpStore } from '@/stores/jump'

const COMPRESS_THRESHOLD = 5 * 1024 * 1024   // 超过即前端压缩
const BACKEND_LIMIT = 10 * 1024 * 1024       // notes.py MAX_IMAGE_BYTES，兜底
const MAX_EDGE = 2560                        // 压缩后最长边
const IMAGE_MIME_EXT: Record<string, string> = {
  'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp',
}

interface NoteMeta {
  id: number; section_id: number; title: string; sort_order: number
  is_private: number; revision: number; updated_at: string; excerpt: string
}
interface Section { id: number; notebook_id: number; name: string; notes: NoteMeta[] }
interface Notebook { id: number; name: string; sections: Section[] }
interface NoteDetail {
  id: number; section_id: number; title: string; content_json: string
  content_text: string; is_private: number; revision: number; updated_at: string
}

const message = useMessage()
const dialog = useDialog()
const jump = useJumpStore()
const books = ref<Notebook[]>([])
const activeBookId = ref<number | null>(null)             // 下拉当前笔记本
const renamingSectionId = ref<number | null>(null)        // 分区就地改名
const renamingValue = ref('')
const bookMenuShow = ref(false)
const activeId = ref<number | null>(null)
const detail = ref<NoteDetail | null>(null)
const title = ref('')
const isPrivate = ref(false)
const revision = ref(0)
const saveState = ref<'saved' | 'saving' | 'dirty'>('saved')
const busy = ref(false)
const imageBusy = ref(0)                    // 待上传 + 上传中的图片数
const tableActive = ref(false)              // 光标是否在表格内（决定菜单项是否可用）
const tableMenuShow = ref(false)
// 自定义行高对话框（表格菜单 → 行高：自定义…）
const showRowHeight = ref(false)
const rowHeightValue = ref<number | null>(36)

const showDialog = ref(false)
const dialogMode = ref<'book' | 'section'>('book')
const dialogTarget = ref<number | null>(null)
const dialogName = ref('')
const dialogAction = ref<'create' | 'rename'>('create')

let saveTimer: number | undefined
let loadingContent = false
let uploadChain: Promise<void> = Promise.resolve()   // 多图串行，保证插入顺序
let disposed = false

/** 表格节点：在原生 Table 上加 rowHeight 属性（px）。
 *
 * Tiptap 没有行高概念（合并单元格下"行高"本身有歧义），这里用整表行高：
 * 属性随 content_json 持久化，渲染成 CSS 变量 --row-h，由单元格 height 生效。
 * 列宽仍走原生 resizable（写 colwidth，随文档保存，换设备不再重算跳动）。
 */
const TableWithHeight = Table.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      rowHeight: {
        default: null,
        parseHTML: el => el.getAttribute('data-row-height'),
        renderHTML: attrs => (attrs.rowHeight
          ? { 'data-row-height': String(attrs.rowHeight), style: `--row-h:${attrs.rowHeight}px` }
          : {}),
      },
    }
  },
})

const editor = useEditor({
  extensions: [
    StarterKit,
    TiptapImage,
    // allowTableNodeSelection 让点表格边缘可整表选中，方便删除 / 剪切。
    TableWithHeight.configure({
      resizable: true,
      lastColumnResizable: true,
      allowTableNodeSelection: true,
    }),
    TableRow,
    TableHeader,
    TableCell,
  ],
  content: '',
  editorProps: {
    attributes: { class: 'tiptap-body' },
    handlePaste: (_view, event) => handlePasteImage(event),
    transformPastedHTML: html => stripRemoteImages(html),
  },
  onUpdate: () => {
    tableActive.value = !!editor.value?.isActive('table')
    if (!loadingContent) markDirty()
  },
  onSelectionUpdate: () => {
    tableActive.value = !!editor.value?.isActive('table')
  },
})

const activeNote = computed(() => {
  for (const b of books.value) {
    for (const s of b.sections) {
      const found = s.notes.find(n => n.id === activeId.value)
      if (found) return found
    }
  }
  return null
})

const activeBook = computed(() =>
  books.value.find(b => b.id === activeBookId.value) || null)

const allSections = computed(() => activeBook.value?.sections ?? [])
/** 顶部页签选中的分区：左侧只列它的页面 */
const activeSectionId = ref<number | null>(null)
const activeSection = computed(() =>
  allSections.value.find(s => s.id === activeSectionId.value) || null)
const pageList = computed(() => activeSection.value?.notes || [])

// ---- 顶部页签：放不下的收进「更多 ▾」（页签栏本身不滚动）----
const tabsEl = ref<HTMLElement | null>(null)
const measureEl = ref<HTMLElement | null>(null)   // 隐藏测量层，量每个页签的自然宽度
const tabWidths = ref<number[]>([])
const tabsWidth = ref(0)
const MORE_WIDTH = 66
let tabObserver: ResizeObserver | null = null

const visibleCount = computed(() => {
  const total = allSections.value.length
  if (!total) return 0
  const width = tabsWidth.value
  if (!width) return total                 // 还没量到：先全显示，避免闪一下「更多」
  const at = (i: number) => tabWidths.value[i] ?? 90
  const fit = (limit: number) => {
    let used = 0
    let count = 0
    for (let i = 0; i < total; i++) {
      if (used + at(i) > limit) break
      used += at(i)
      count++
    }
    return count
  }
  const full = fit(width)
  if (full >= total) return total          // 全放得下，不需要「更多」
  return Math.max(1, fit(width - MORE_WIDTH))
})

const visibleSections = computed(() => {
  const all = allSections.value
  const count = visibleCount.value
  if (!all.length || count >= all.length) return all
  const head = all.slice(0, Math.max(1, count))
  // 当前分区若被挤进「更多」，把它换进可见区末尾，否则看不到激活态
  const index = all.findIndex(s => s.id === activeSectionId.value)
  if (index >= count) head[head.length - 1] = all[index]
  return head
})

const hiddenSections = computed(() => {
  const shown = new Set(visibleSections.value.map(s => s.id))
  return allSections.value.filter(s => !shown.has(s.id))
})

const moreOptions = computed(() =>
  hiddenSections.value.map(s => ({ label: s.name, key: s.id })))

function onMoreSelect(key: string | number) {
  void selectSection(Number(key))
}

async function measureTabs() {
  await nextTick()
  const el = tabsEl.value
  const measure = measureEl.value
  if (!el || !measure) return
  tabsWidth.value = el.clientWidth
  tabWidths.value = Array.from(measure.children)
    .map(node => (node as HTMLElement).offsetWidth + 4)   // 4 = flex gap
}

function locateNote(id: number) {
  for (const book of books.value) {
    for (const section of book.sections) {
      if (section.notes.some(n => n.id === id)) return { book, section }
    }
  }
  return null
}

/** 下拉换笔记本：分区页签跟着换，并关掉不属于新本的页面 */
function onBookChange(value: string | number | null) {
  if (value == null) return
  const id = Number(value)
  activeBookId.value = id
  const first = books.value.find(b => b.id === id)?.sections[0]
  activeSectionId.value = first?.id ?? null
  activeId.value = null
  detail.value = null
  void measureTabs()
}

/** 切分区：当前页面不属于新分区就关掉，避免右侧留着别的分区的内容 */
async function selectSection(id: number) {
  if (activeSectionId.value === id) return
  if (saveState.value === 'dirty') await save()
  activeSectionId.value = id
  if (activeId.value && !(activeSection.value?.notes || []).some(n => n.id === activeId.value)) {
    if (saveTimer) window.clearTimeout(saveTimer)
    activeId.value = null
    detail.value = null
  }
}

const bookMenuOptions = [
  { label: '新建笔记本', key: 'create' },
  { label: '重命名当前笔记本', key: 'rename' },
  { label: '删除当前笔记本', key: 'delete' },
]

/** 表格菜单：光标不在表格内时，行/列/合并类操作置灰（命令本身也会空跑）。 */
type TableMenuItem =
  | { label: string; key: string; disabled?: boolean }
  | { type: 'divider'; key: string }

const tableMenuOptions = computed<TableMenuItem[]>(() => {
  const inTable = tableActive.value
  return [
    { label: '插入 3×3 表格', key: 'insert' },
    { type: 'divider', key: 'd1' },
    { label: '上方插入行', key: 'row-before', disabled: !inTable },
    { label: '下方插入行', key: 'row-after', disabled: !inTable },
    { label: '左侧插入列', key: 'col-before', disabled: !inTable },
    { label: '右侧插入列', key: 'col-after', disabled: !inTable },
    { type: 'divider', key: 'd2' },
    { label: '合并单元格', key: 'merge', disabled: !inTable },
    { label: '拆分单元格', key: 'split', disabled: !inTable },
    { type: 'divider', key: 'd3' },
    { label: '行高：紧凑 26px', key: 'h-compact', disabled: !inTable },
    { label: '行高：标准 36px', key: 'h-normal', disabled: !inTable },
    { label: '行高：宽松 52px', key: 'h-loose', disabled: !inTable },
    { label: '行高：自定义…', key: 'h-custom', disabled: !inTable },
    { type: 'divider', key: 'd4' },
    { label: '删除当前行', key: 'row-delete', disabled: !inTable },
    { label: '删除当前列', key: 'col-delete', disabled: !inTable },
    { label: '删除整个表格', key: 'delete', disabled: !inTable },
  ]
})

function onTableMenuSelect(key: string | number) {
  const ed = editor.value
  if (!ed) return
  const c = () => ed.chain().focus()
  if (key === 'insert') c().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()
  else if (key === 'row-before') c().addRowBefore().run()
  else if (key === 'row-after') c().addRowAfter().run()
  else if (key === 'col-before') c().addColumnBefore().run()
  else if (key === 'col-after') c().addColumnAfter().run()
  else if (key === 'merge') c().mergeCells().run()
  else if (key === 'split') c().splitCell().run()
  else if (key === 'h-compact') c().updateAttributes('table', { rowHeight: 26 }).run()
  else if (key === 'h-normal') c().updateAttributes('table', { rowHeight: 36 }).run()
  else if (key === 'h-loose') c().updateAttributes('table', { rowHeight: 52 }).run()
  else if (key === 'h-custom') {
    rowHeightValue.value = 36
    showRowHeight.value = true
  }
  else if (key === 'row-delete') c().deleteRow().run()
  else if (key === 'col-delete') c().deleteColumn().run()
  else if (key === 'delete') c().deleteTable().run()
}

/** 自定义行高：夹在 18–400px，避免手滑输入出极端值 */
function applyRowHeight() {
  const px = Math.min(400, Math.max(18, Number(rowHeightValue.value) || 36))
  editor.value?.chain().focus().updateAttributes('table', { rowHeight: px }).run()
  showRowHeight.value = false
}

// ---------------------------------------------------------------- OneNote 导入

interface ImportSection {
  id: number; name: string; pages: number; first_note_id: number | null
}
interface ImportResp {
  notebook_id: number; sections: ImportSection[]; pages: number
  images: number; missing: string[]; errors: { file: string; error: string }[]
}

const MAX_IMPORT_FILES = 20
const MAX_IMPORT_BYTES = 64 * 1024 * 1024

const importing = ref(false)
const importMenuShow = ref(false)
const importProgress = ref({ done: 0, total: 0, current: '' })
const showImportResult = ref(false)
const importResult = ref<ImportResp | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const dirInput = ref<HTMLInputElement | null>(null)

/** 顶部「更多」：导入 / 导出 / 备份三组。
 *  导出范围由当前打开页面推导（页面 → 分区 → 笔记本），没有页面时置灰。 */
type FileMenuItem =
  | { label: string; key: string; disabled?: boolean }
  | { type: 'divider'; key: string }

const fileMenuOptions = computed<FileMenuItem[]>(() => {
  const noBook = !activeBookId.value || importing.value
  const noNote = !activeNote.value
  return [
    { label: '导入 OneNote .mht 文件（可多选）', key: 'import-files', disabled: noBook },
    { label: '导入文件夹', key: 'import-dir', disabled: noBook },
    { type: 'divider', key: 'fd1' },
    { label: '导出本页 Markdown（.md）', key: 'export-page-md', disabled: !activeId.value },
    { label: '导出本页 HTML + 图片（.zip）', key: 'export-page-zip', disabled: !activeId.value },
    { label: '导出当前分区（.zip）', key: 'export-section', disabled: noNote },
    { label: '导出整个笔记本（.zip）', key: 'export-notebook', disabled: noNote },
    { type: 'divider', key: 'fd2' },
    { label: '备份全库（写 data/backups）', key: 'backup' },
  ]
})

function onFileMenuSelect(key: string | number) {
  if (key === 'import-files') {
    if (!importing.value) fileInput.value?.click()
  } else if (key === 'import-dir') {
    if (!importing.value) dirInput.value?.click()
  } else if (key === 'export-page-md') {
    void exportTo('page', 'md')
  } else if (key === 'export-page-zip') {
    void exportTo('page', 'zip')
  } else if (key === 'export-section') {
    void exportTo('section', 'zip')
  } else if (key === 'export-notebook') {
    void exportTo('notebook', 'zip')
  } else if (key === 'backup') {
    void backup()
  }
}

function pickMht(list: FileList | null): File[] {
  return Array.from(list || []).filter(f => /\.(mht|mhtml)$/i.test(f.name))
}

async function onImportFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = pickMht(input.files)
  input.value = ''                       // 允许连续选同一批文件
  if (!files.length) { message.warning('没有 .mht / .mhtml 文件'); return }
  if (files.length > MAX_IMPORT_FILES) {
    message.warning(`单次最多 ${MAX_IMPORT_FILES} 个文件，当前选了 ${files.length} 个`)
    return
  }
  if (!activeBookId.value) { message.warning('请先选择一个笔记本'); return }
  await runImport(files, activeBookId.value)
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const text = String(reader.result || '')
      const comma = text.indexOf(',')
      resolve(comma >= 0 ? text.slice(comma + 1) : text)
    }
    reader.onerror = () => reject(new Error('读取文件失败：' + file.name))
    reader.readAsDataURL(file)
  })
}

/** 逐个文件串行导入：至少能给出文件级进度，也能把单个失败隔离在它自己身上。 */
async function runImport(files: File[], notebookId: number) {
  importing.value = true
  const agg: ImportResp = {
    notebook_id: notebookId, sections: [], pages: 0, images: 0, missing: [], errors: [],
  }
  try {
    for (let i = 0; i < files.length; i++) {
      const file = files[i]
      importProgress.value = { done: i, total: files.length, current: file.name }
      if (file.size > MAX_IMPORT_BYTES) {
        agg.errors.push({ file: file.name, error: '超过 64 MB 上限' })
        continue
      }
      try {
        const data = await fileToBase64(file)
        const r = await api<ImportResp>('/api/notes/import-mht', {
          method: 'POST',
          body: JSON.stringify({
            notebook_id: notebookId,
            files: [{ name: file.name, data }],
          }),
        })
        agg.sections.push(...r.sections)
        agg.pages += r.pages
        agg.images += r.images
        agg.missing.push(...r.missing)
        agg.errors.push(...r.errors)
      } catch (e) {
        agg.errors.push({ file: file.name, error: String(e) })
      }
    }
    importProgress.value = { done: files.length, total: files.length, current: '' }
    importResult.value = agg
    showImportResult.value = true
    await loadTree()
    const last = agg.sections[agg.sections.length - 1]
    if (last?.first_note_id) await openNote(last.first_note_id)
  } finally {
    importing.value = false
  }
}

async function loadTree() {
  try {
    books.value = await api<Notebook[]>('/api/notebooks')
    // 当前笔记本被删 / 首次加载时落到第一本
    if (!books.value.some(b => b.id === activeBookId.value)) {
      activeBookId.value = books.value[0]?.id ?? null
    }
    // 当前分区被删（或换了笔记本）时落回第一个分区
    const sections = activeBook.value?.sections || []
    if (!sections.some(s => s.id === activeSectionId.value)) {
      activeSectionId.value = sections[0]?.id ?? null
    }
    void measureTabs()
  } catch (e) {
    message.error(String(e))
  }
}

// ---------------------------------------------------------------- 拖拽排序
// 分区在笔记本内调序（PUT /api/sections/{id}）；页面在分区内调序，
// 也允许拖到别的分区页签上跨分区移动（POST /api/notes/{id}/move）。
// 排序值统一重排为 1..n，避免多次拖拽后出现重复 / 稀疏的 sort_order。

const dragSectionId = ref<number | null>(null)
const dragNoteId = ref<number | null>(null)
const dropSectionId = ref<number | null>(null)
const dropNoteId = ref<number | null>(null)

function beginDrag(e: DragEvent, id: number) {
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', String(id))
  }
}

function onDragEnd() {
  dragSectionId.value = null
  dragNoteId.value = null
  dropSectionId.value = null
  dropNoteId.value = null
}

async function persistSectionOrder(ids: number[]) {
  const book = activeBook.value
  if (!book) return
  const byId = new Map(book.sections.map(s => [s.id, s]))
  try {
    await Promise.all(ids.map((id, index) => {
      const section = byId.get(id)
      if (!section) return Promise.resolve(null)
      return api(`/api/sections/${id}`, {
        method: 'PUT',
        body: JSON.stringify({
          notebook_id: book.id, name: section.name, sort_order: index + 1,
        }),
      })
    }))
    await loadTree()
  } catch (e) {
    message.error('调整分区顺序失败：' + String(e))
    await loadTree()
  }
}

async function persistNoteOrder(sectionId: number, ids: number[]) {
  try {
    await Promise.all(ids.map((id, index) => api(`/api/notes/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ section_id: sectionId, sort_order: index + 1 }),
    })))
    await loadTree()
  } catch (e) {
    message.error('调整页面顺序失败：' + String(e))
    await loadTree()
  }
}

function onSectionDragStart(s: Section, e: DragEvent) {
  if (renamingSectionId.value === s.id) return
  dragSectionId.value = s.id
  beginDrag(e, s.id)
}

function onSectionDragOver(s: Section, e: DragEvent) {
  if (dragSectionId.value === null && dragNoteId.value === null) return
  e.preventDefault()
  dropSectionId.value = s.id
}

async function onSectionDrop(s: Section, e: DragEvent) {
  e.preventDefault()
  const movingNote = dragNoteId.value
  const fromSection = dragSectionId.value
  dropSectionId.value = null
  if (movingNote) {
    // 页面拖到分区页签：跨分区移动，顺序排到目标分区末尾
    if (s.id === activeSectionId.value) { onDragEnd(); return }
    onDragEnd()
    try {
      await api(`/api/notes/${movingNote}/move`, {
        method: 'POST',
        body: JSON.stringify({ section_id: s.id, sort_order: (s.notes.length || 0) + 1 }),
      })
      await loadTree()
      selectSection(s.id)
      message.success(`已移动到「${s.name}」`)
    } catch (err) {
      message.error('移动页面失败：' + String(err))
    }
    return
  }
  onDragEnd()
  if (!fromSection || fromSection === s.id) return
  const ids = (activeBook.value?.sections || []).map(x => x.id)
  const from = ids.indexOf(fromSection)
  const to = ids.indexOf(s.id)
  if (from < 0 || to < 0) return
  ids.splice(to, 0, ids.splice(from, 1)[0])
  await persistSectionOrder(ids)
}

function onNoteDragStart(n: NoteMeta, e: DragEvent) {
  dragNoteId.value = n.id
  beginDrag(e, n.id)
}

function onNoteDragOver(n: NoteMeta, e: DragEvent) {
  if (dragNoteId.value === null || dragNoteId.value === n.id) return
  e.preventDefault()
  dropNoteId.value = n.id
}

async function onNoteDrop(n: NoteMeta, e: DragEvent) {
  e.preventDefault()
  const moving = dragNoteId.value
  dropNoteId.value = null
  onDragEnd()
  const sectionId = activeSectionId.value
  if (!moving || moving === n.id || !sectionId) return
  const ids = (activeSection.value?.notes || []).map(x => x.id)
  const from = ids.indexOf(moving)
  const to = ids.indexOf(n.id)
  if (from < 0 || to < 0) return
  ids.splice(to, 0, ids.splice(from, 1)[0])
  await persistNoteOrder(sectionId, ids)
}

function onBookMenuSelect(key: string | number) {  const book = activeBook.value
  if (key === 'create') openDialog('book')
  else if (key === 'rename' && book) openDialog('book', book.id, book.name, 'rename')
  else if (key === 'delete' && book) confirmDelete('notebook', book.id, book.name)
}

async function startRenameSection(section: Section) {
  renamingSectionId.value = section.id
  renamingValue.value = section.name
  await nextTick()
  const input = document.querySelector<HTMLInputElement>('.rename-input')
  input?.focus()
  input?.select()
}

async function commitRenameSection() {
  const id = renamingSectionId.value
  const name = renamingValue.value.trim()
  renamingSectionId.value = null                  // 先清空，防止 Enter 与 blur 双触发
  if (!id || !name) return
  const bookId = books.value.find(b => b.sections.some(s => s.id === id))?.id
  if (!bookId) return
  try {
    await api(`/api/sections/${id}`, {
      method: 'PUT', body: JSON.stringify({ name, notebook_id: bookId }),
    })
    await loadTree()
  } catch (e) {
    message.error('分区改名失败：' + String(e))
  }
}

function cancelRenameSection() {
  renamingSectionId.value = null
}

function onSectionContext(event: MouseEvent, section: Section) {
  event.preventDefault()
  confirmDelete('section', section.id, section.name)
}

function onNoteContext(event: MouseEvent, note: NoteMeta) {
  event.preventDefault()
  confirmDelete('note', note.id, note.title || '未命名页面')
}

function markDirty() {
  saveState.value = 'dirty'
  if (saveTimer) window.clearTimeout(saveTimer)
  saveTimer = window.setTimeout(save, 1000)
}

async function save() {
  if (!detail.value || !editor.value) return
  saveState.value = 'saving'
  try {
    const r = await api<{ revision: number; saved_at: string }>(
      `/api/notes/${detail.value.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          title: title.value,
          content_json: JSON.stringify(editor.value.getJSON()),
          is_private: isPrivate.value,
          revision: revision.value,
        }),
      })
    revision.value = r.revision
    saveState.value = 'saved'
    await loadTree()
  } catch (e) {
    saveState.value = 'dirty'
    message.error('保存失败（可能 revision 冲突，请重新打开页面）：' + String(e))
  }
}

async function openNote(id: number) {
  if (saveState.value === 'dirty' && activeId.value !== id) await save()
  // 页面可能属于别的笔记本 / 分区（深链接、导入跳转），先把页签切过去
  const hit = locateNote(id)
  if (hit) {
    if (activeBookId.value !== hit.book.id) {
      activeBookId.value = hit.book.id
      void measureTabs()
    }
    if (activeSectionId.value !== hit.section.id) activeSectionId.value = hit.section.id
  }
  activeId.value = id
  busy.value = true
  try {
    const d = await api<NoteDetail>(`/api/notes/${id}`)
    detail.value = d
    title.value = d.title
    isPrivate.value = !!d.is_private
    revision.value = d.revision
    loadingContent = true
    const doc = d.content_json ? JSON.parse(d.content_json) : ''
    editor.value?.commands.setContent(doc || '')
    loadingContent = false
    saveState.value = 'saved'
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

async function focusNote(id: number) {
  // 项目工作台 / 深链接跳过来：openNote 会自己定位笔记本与分区页签
  await openNote(id)
}

async function createNote(sectionId: number) {
  try {
    const r = await api<{ id: number }>('/api/notes', {
      method: 'POST', body: JSON.stringify({ section_id: sectionId, title: '未命名页面' }),
    })
    await loadTree()
    await openNote(r.id)
  } catch (e) {
    message.error(String(e))
  }
}

function openDialog(mode: 'book' | 'section', target: number | null = null,
                    name = '', action: 'create' | 'rename' = 'create') {
  dialogMode.value = mode
  dialogTarget.value = target
  dialogName.value = name
  dialogAction.value = action
  showDialog.value = true
}

async function submitDialog() {
  const name = dialogName.value.trim()
  if (!name) return
  try {
    const isRename = dialogAction.value === 'rename'
    if (dialogMode.value === 'book') {
      if (isRename && dialogTarget.value) {
        await api(`/api/notebooks/${dialogTarget.value}`, {
          method: 'PUT', body: JSON.stringify({ name }),
        })
      } else {
        await api('/api/notebooks', { method: 'POST', body: JSON.stringify({ name }) })
      }
    } else if (isRename && dialogTarget.value) {
      const bookId = books.value.find(
        b => b.sections.some(s => s.id === dialogTarget.value))?.id
      await api(`/api/sections/${dialogTarget.value}`, {
        method: 'PUT', body: JSON.stringify({ name, notebook_id: bookId }),
      })
    } else {
      // 新建分区时 target 是 notebook_id
      await api('/api/sections', {
        method: 'POST', body: JSON.stringify({ name, notebook_id: dialogTarget.value }),
      })
    }
    showDialog.value = false
    await loadTree()
  } catch (e) {
    message.error(String(e))
  }
}

function confirmDelete(kind: 'notebook' | 'section' | 'note', id: number, label: string) {
  dialog.warning({
    title: '确认删除',
    content: `删除「${label}」后，其下的分区与页面会一并删除且不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api(`/api/${kind === 'notebook' ? 'notebooks' : kind === 'section' ? 'sections' : 'notes'}/${id}`,
                  { method: 'DELETE' })
        if (activeId.value === id || kind !== 'note') {
          if (saveTimer) window.clearTimeout(saveTimer)
          activeId.value = null
          detail.value = null
        }
        await loadTree()
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

function onPickImage(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  const noteId = activeId.value
  if (file && noteId) enqueueImage(file, noteId, editor.value?.state.selection.from)
  input.value = ''
}

/** 超过 5 MiB 的位图等比缩放 + 质量递减；透明图保留 PNG，GIF 不动（会丢帧）。 */
async function compressImage(file: File): Promise<File> {
  if (file.type === 'image/gif') return file
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    return file                        // 不支持解码时退回原图，由后端 10 MB 兜底
  }
  try {
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const w = Math.max(1, Math.round(bitmap.width * scale))
    const h = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    const ctx = canvas.getContext('2d')
    if (!ctx) return file
    ctx.drawImage(bitmap, 0, 0, w, h)
    let hasAlpha = false
    if (file.type === 'image/png' || file.type === 'image/webp') {
      const rgba = ctx.getImageData(0, 0, w, h).data
      for (let i = 3; i < rgba.length; i += 4) {
        if (rgba[i] < 255) { hasAlpha = true; break }
      }
    }
    const mime = hasAlpha ? 'image/png' : 'image/jpeg'
    let quality = 0.85
    let blob: Blob | null = null
    for (let attempt = 0, attempts = hasAlpha ? 1 : 6; attempt < attempts; attempt++) {
      blob = await new Promise<Blob | null>(resolve =>
        canvas.toBlob(resolve, mime, hasAlpha ? undefined : quality))
      if (blob && blob.size <= COMPRESS_THRESHOLD) break
      quality -= 0.12
      if (quality < 0.3) break
    }
    if (!blob || blob.size > BACKEND_LIMIT) return file
    const ext = IMAGE_MIME_EXT[mime]
    return new File([blob], (file.name.replace(/\.[^.]+$/, '') || 'image') + '.' + ext,
                    { type: mime })
  } finally {
    bitmap.close()
  }
}

async function insertImageFile(file: File, noteId: number, anchor?: number): Promise<boolean> {
  if (!IMAGE_MIME_EXT[file.type]) {
    message.warning('已忽略 1 张不受支持的图片（仅 png / jpg / gif / webp）')
    return false
  }
  let target = file
  if (file.size > COMPRESS_THRESHOLD) {
    try {
      target = await compressImage(file)
    } catch (e) {
      message.warning('图片压缩失败，改用原图：' + String(e))
    }
  }
  if (target.size > BACKEND_LIMIT) {
    message.warning(`图片超过 10 MB 上限，已跳过：${target.name}`)
    return false
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(target)
  })
  const r = await api<{ url: string }>(`/api/notes/${noteId}/images`, {
    method: 'POST', body: JSON.stringify({ data: dataUrl, name: target.name }),
  })
  // 上传是异步的；期间若已切页或组件卸载，绝不把图插进新页面
  if (disposed || activeId.value !== noteId || !editor.value) return false
  // 显式位置插入：setImage 是 setNode（替换当前节点），insertContent 在
  // 插入 image 后会把选区变成 NodeSelection，下一次插入仍会替换掉它。
  // 用 insertContentAt 指定位置，配合队列偏移让同一批图片依次排开。
  const ed = editor.value
  const max = ed.state.doc.content.size
  const pos = anchor === undefined ? max : Math.min(anchor, max)
  ed.chain().focus().insertContentAt(pos, { type: 'image', attrs: { src: r.url } }).run()
  return true
}

/** 串行队列：多图按粘贴顺序插入，同批按 anchor + 队列偏移依次排开。 */
function enqueueImage(file: File, noteId: number, anchor?: number) {
  const offset = imageBusy.value
  imageBusy.value++
  uploadChain = uploadChain.then(async () => {
    try {
      await insertImageFile(file, noteId, anchor === undefined ? undefined : anchor + offset)
    } catch (e) {
      message.error('图片插入失败：' + String(e))
    } finally {
      imageBusy.value = Math.max(0, imageBusy.value - 1)
    }
  })
}

/** 截图类粘贴：剪贴板直接给位图文件时接管，纯文本 / 图文不接管以免丢字。 */
function handlePasteImage(event: ClipboardEvent): boolean {
  const dt = event.clipboardData
  const noteId = activeId.value
  if (!dt || !noteId || !detail.value) return false
  const html = dt.getData('text/html') || ''
  const rich = /<img\b|<p\b|<div\b|<span\b|<br\b/i.test(html)
  if (rich) return false               // 交给默认粘贴 + transformPastedHTML
  const files: File[] = []
  for (const item of Array.from(dt.items)) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const f = item.getAsFile()
      if (f) files.push(f)
    }
  }
  if (!files.length) return false
  const anchor = editor.value?.state.selection.from
  event.preventDefault()
  for (const f of files) enqueueImage(f, noteId, anchor)
  return true
}

function normalizeMime(blob: Blob, src: string): string {
  if (IMAGE_MIME_EXT[blob.type]) return blob.type
  const m = /\.(png|jpe?g|gif|webp)(?:[?#]|$)/i.exec(src)
  if (!m) return ''
  const ext = m[1].toLowerCase()
  return ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg' : 'image/' + ext
}

function dataUrlToFile(src: string): File | null {
  const m = /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$/i.exec(src)
  if (!m) return null
  const mime = m[1].toLowerCase()
  if (!IMAGE_MIME_EXT[mime]) return null   // svg+xml 等一律不接（后端魔数也会拒）
  try {
    const bin = atob(m[2].replace(/\s/g, ''))
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    return new File([bytes], 'pasted.' + IMAGE_MIME_EXT[mime], { type: mime })
  } catch {
    return null
  }
}

/** 外链图尝试前端转存；CORS / 404 等失败时按约定降级忽略并提示。 */
async function fetchAndEnqueue(src: string, noteId: number) {
  try {
    const res = await fetch(src)
    if (!res.ok) throw new Error(String(res.status))
    const blob = await res.blob()
    if (blob.size > BACKEND_LIMIT) throw new Error('超过 10 MB')
    const mime = normalizeMime(blob, src)
    if (!mime) throw new Error('非受支持图片格式')
    enqueueImage(new File([blob], 'pasted.' + IMAGE_MIME_EXT[mime], { type: mime }), noteId)
  } catch {
    message.warning('已忽略 1 张外链图片（无法读取，通常因跨域限制）')
  }
}

/**
 * 图文混排粘贴：先剥离外链 / data: / 本地路径图片，再让 TipTap 正常插入文字。
 * 不剥离会让外链 src 原样进 content_json（导出时被置空 → 丢图），
 * data: 图则被 extension-image 的 allowBase64=false 直接丢弃。
 */
function stripRemoteImages(html: string): string {
  const noteId = activeId.value
  if (!noteId || html.indexOf('<img') === -1) return html
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const imgs = Array.from(doc.querySelectorAll('img'))
  let removed = 0
  for (const img of imgs) {
    const src = img.getAttribute('src') || ''
    if (src.startsWith('/api/attachments/')) continue   // 本库附件原样保留
    img.remove()
    removed++
    if (/^data:image\//i.test(src)) {
      const f = dataUrlToFile(src)
      if (f) enqueueImage(f, noteId)
      else message.warning('已忽略 1 张无法解析的内联图片')
    } else if (/^https?:\/\//i.test(src) || src.startsWith('blob:')) {
      void fetchAndEnqueue(src, noteId)
    } else {
      message.warning('已忽略 1 张本地路径图片（浏览器不允许读取）')
    }
  }
  return removed ? doc.body.innerHTML : html
}

function exportTarget(scope: 'page' | 'section' | 'notebook'): number {
  if (scope === 'page') return activeId.value ?? 0
  if (scope === 'section') return activeNote.value?.section_id ?? 0
  const sectionId = activeNote.value?.section_id
  return books.value.find(b => b.sections.some(s => s.id === sectionId))?.id ?? 0
}

/** 浏览器模式回退：让浏览器下载到它自己的下载目录（桌面窗口里下载默认被禁）。 */
async function downloadViaBrowser(scope: string, id: number, format: string) {
  const res = await fetch(
    `/api/notes/export?scope=${scope}&id=${id}&format=${format}`,
    { headers: { 'X-OnlyOne-Token': TOKEN } })
  if (!res.ok) throw new Error(String(res.status))
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `onlyone-${scope}.${format === 'md' ? 'md' : format === 'html' ? 'html' : 'zip'}`
  a.click()
  URL.revokeObjectURL(url)
  message.success('已交给浏览器下载，请到「下载」文件夹查看')
}

/**
 * 导出：桌面端弹系统「另存为」，由后端写盘并返回真实路径。
 * pywebview 的 ALLOW_DOWNLOADS 默认关闭，前端 <a download> 在窗口里点了没有反应，
 * 所以桌面模式必须走 /api/notes/export/save；该接口在浏览器模式下返回 409 → 回退下载。
 */
async function exportTo(scope: 'page' | 'section' | 'notebook', format: 'md' | 'html' | 'zip') {
  const id = exportTarget(scope)
  if (!id) { message.warning('请先选择一个页面'); return }
  try {
    const res = await fetch('/api/notes/export/save', {
      method: 'POST',
      headers: { 'X-OnlyOne-Token': TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, id, format }),
    })
    if (res.status === 409) {
      await downloadViaBrowser(scope, id, format)
      return
    }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
    const data = await res.json() as { saved: string | null; canceled?: boolean; size?: number }
    if (data.canceled || !data.saved) return
    message.success(
      `已保存：${data.saved}（${((data.size || 0) / 1024).toFixed(0)} KB）`,
      { duration: 8000 })
  } catch (e) {
    const msg = String(e)
    // WebView2 调用系统对话框失败时退回浏览器下载，至少不丢功能
    if (msg.includes('500')) {
      try { await downloadViaBrowser(scope, id, format); return } catch { /* 继续报错 */ }
    }
    message.error('导出失败：' + msg)
  }
}

async function backup() {
  if (saveState.value === 'dirty') await save()
  try {
    const r = await api<{ file: string; size: number }>('/api/backup', { method: 'POST' })
    message.success(`已备份 ${r.file}（${(r.size / 1024 / 1024).toFixed(1)} MB）到 data/backups/`)
  } catch (e) {
    message.error(String(e))
  }
}

watch([title, isPrivate], () => {
  if (detail.value && !loadingContent) markDirty()
})

onMounted(async () => {
  await loadTree()
  // 页签栏宽度变化（窗口缩放 / 侧栏折叠）后重新决定谁进「更多」
  await nextTick()
  if (tabsEl.value) {
    tabObserver = new ResizeObserver(() => { void measureTabs() })
    tabObserver.observe(tabsEl.value)
  }
  // 支持 ?tab=notes&note=<id> 深链接（跨页跳转与 headless 截图用）
  const noteParam = new URLSearchParams(window.location.search).get('note')
  if (noteParam && /^\d+$/.test(noteParam)) await focusNote(Number(noteParam))
  // 项目工作台「记事本」页签点标题跳过来：store 里带目标页 id
  const pendingNote = jump.consumeNoteId()
  if (pendingNote) await focusNote(pendingNote)
})
onBeforeUnmount(() => {
  disposed = true
  tabObserver?.disconnect()
  if (saveTimer) window.clearTimeout(saveTimer)
  editor.value?.destroy()
})
</script>

<template>
  <div class="notes-layout">
    <!-- 顶部固定：笔记本 + 分区页签（放不下的收进「更多」，页签栏自身不滚动） -->
    <div class="notes-topbar">
      <n-select
        :value="activeBookId"
        :options="books.map(b => ({ label: b.name, value: b.id }))"
        size="tiny"
        style="width: 140px; flex: 0 0 auto"
        placeholder="选择笔记本"
        @update:value="onBookChange"
      />
      <n-dropdown
        v-model:show="bookMenuShow"
        :options="bookMenuOptions"
        trigger="click"
        @select="onBookMenuSelect"
      >
        <n-button size="tiny" quaternary title="笔记本：新建 / 改名 / 删除">⋯</n-button>
      </n-dropdown>

      <div ref="tabsEl" class="tabs">
        <button
          v-for="s in visibleSections" :key="s.id" class="tab"
          :class="{ active: s.id === activeSectionId, 'drop-target': dropSectionId === s.id }"
          :title="`${s.name} · 双击改名 · 右键删除 · 拖动调整顺序`"
          draggable="true"
          @click="selectSection(s.id)"
          @dblclick.stop="startRenameSection(s)"
          @contextmenu.prevent="onSectionContext($event, s)"
          @dragstart="onSectionDragStart(s, $event)"
          @dragover="onSectionDragOver(s, $event)"
          @drop="onSectionDrop(s, $event)"
          @dragend="onDragEnd"
        >
          <input
            v-if="renamingSectionId === s.id"
            v-model="renamingValue"
            class="rename-input tab-rename"
            @keyup.enter="commitRenameSection"
            @keyup.esc="cancelRenameSection"
            @blur="commitRenameSection"
            @click.stop
          />
          <span v-else class="tab-name">{{ s.name }}</span>
        </button>
        <n-dropdown
          v-if="hiddenSections.length"
          :options="moreOptions"
          trigger="click"
          @select="onMoreSelect"
        >
          <button class="tab tab-more" title="更多分区">⋯</button>
        </n-dropdown>
        <span v-if="!books.length" class="muted">还没有笔记本，点 ⋯ 新建</span>
        <span v-else-if="activeBook && !allSections.length" class="muted">还没有分区</span>
      </div>

      <n-dropdown
        v-model:show="importMenuShow"
        :options="fileMenuOptions"
        trigger="click"
        @select="onFileMenuSelect"
      >
        <n-button size="tiny" quaternary title="导入 / 导出 / 备份">
          {{ importing ? '导入中…' : '更多 ▾' }}
        </n-button>
      </n-dropdown>
      <n-button v-if="activeBook" size="tiny" quaternary
                @click="openDialog('section', activeBook.id)">+分区</n-button>

      <input ref="fileInput" type="file" accept=".mht,.mhtml,message/rfc822"
             multiple hidden @change="onImportFiles">
      <input ref="dirInput" type="file" v-bind="{ webkitdirectory: '' }"
             hidden @change="onImportFiles">
    </div>

    <div v-if="importing" class="import-progress">
      正在导入 {{ Math.min(importProgress.done + 1, importProgress.total) }}/{{ importProgress.total }}：
      <span class="ellipsis">{{ importProgress.current }}</span>
    </div>

    <div class="notes-body">
      <!-- 左：当前分区的页面列表，独立滚动 -->
      <aside class="card pages">
        <div class="row pages-head">
          <span class="grow ellipsis muted" :title="activeSection?.name">
            {{ activeSection ? activeSection.name : '页面' }}
          </span>
          <n-button size="tiny" quaternary :disabled="!activeSection"
                    @click="activeSection && createNote(activeSection.id)">+页面</n-button>
        </div>
        <div class="page-list">
          <div
            v-for="n in pageList" :key="n.id" class="note-row"
            :class="{ active: activeId === n.id, 'drop-target': dropNoteId === n.id }"
            draggable="true"
            @click="openNote(n.id)"
            @contextmenu="onNoteContext($event, n)"
            @dragstart="onNoteDragStart(n, $event)"
            @dragover="onNoteDragOver(n, $event)"
            @drop="onNoteDrop(n, $event)"
            @dragend="onDragEnd"
          >
            <span class="grow ellipsis">{{ n.title || '未命名页面' }}</span>
            <n-tag v-if="n.is_private" size="tiny" :bordered="false">私密</n-tag>
          </div>
          <div v-if="!pageList.length" class="muted page-empty">
            {{ activeSection ? '该分区还没有页面，点右上角「+页面」' : '先在顶部新建一个分区' }}
          </div>
        </div>
      </aside>

    <section class="card editor-pane">
      <div v-if="!detail" class="editor-empty">
        <empty-state
          title="还没有打开页面"
          desc="从左侧选一个页面开始写；如果还没有笔记本，先点 ⋯ 建一个笔记本，再建分区和页面。"
          hint="正文支持 Ctrl+V 直接粘贴截图与 Excel 表格"
        />
      </div>
      <template v-else>
        <div class="row">
          <n-input v-model:value="title" class="grow" placeholder="页面标题" />
          <span class="muted">不参与检索</span>
          <n-switch v-model:value="isPrivate" size="small" />
          <n-tag size="small" :bordered="false" :type="saveState === 'saved' ? 'success' : 'warning'">
            {{ saveState === 'saved' ? '已保存' : saveState === 'saving' ? '保存中' : '待保存' }}
          </n-tag>
        </div>
        <div class="row toolbar">
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleBold().run()">B</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleItalic().run()">I</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleHeading({ level: 2 }).run()">H2</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleBulletList().run()">列表</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleCodeBlock().run()">代码</n-button>
          <n-dropdown
            v-model:show="tableMenuShow"
            :options="tableMenuOptions"
            trigger="click"
            @select="onTableMenuSelect"
          >
            <n-button size="tiny" secondary title="插入表格 / 增删行列 / 合并单元格">表格</n-button>
          </n-dropdown>
          <label class="image-btn">
            图片
            <input type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden @change="onPickImage">
          </label>
          <span v-if="imageBusy" class="muted">图片处理中 {{ imageBusy }}</span>
          <div class="grow" />
          <span class="muted">
            可 Ctrl+V 粘贴图片 / Excel / 网页表格 · 改动后 1 秒自动保存 · revision {{ revision }}
          </span>
        </div>
        <editor-content :editor="editor" class="tiptap" />
      </template>
    </section>
    </div>

    <!-- 隐藏测量层：量每个页签的自然宽度，决定谁进「更多」 -->
    <div ref="measureEl" class="tab-measure" aria-hidden="true">
      <button v-for="s in allSections" :key="'m' + s.id" class="tab">
        <span class="tab-name">{{ s.name }}</span>
      </button>
    </div>
  </div>

  <n-modal v-model:show="showRowHeight" preset="card" title="表格行高" style="width: 300px">
    <div class="row" style="gap: 8px; align-items: center">
      <n-input-number v-model:value="rowHeightValue" :min="18" :max="400" :step="2"
                      size="small" style="width: 130px" />
      <span class="muted">px（18–400，整表生效）</span>
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end; gap: 8px">
        <n-button size="small" @click="showRowHeight = false">取消</n-button>
        <n-button size="small" type="primary" @click="applyRowHeight">应用</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showDialog" preset="card" style="width: 400px"
           :title="dialogMode === 'book' ? '笔记本' : '分区'">
    <n-input v-model:value="dialogName" placeholder="名称" @keyup.enter="submitDialog" />
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showDialog = false">取消</n-button>
        <n-button type="primary" @click="submitDialog">确定</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showImportResult" preset="card" style="width: 520px"
           title="OneNote 导入结果">
    <div v-if="importResult" class="import-result">
      <div class="row"><span class="muted">新建分区</span><span class="grow" />
        <b>{{ importResult.sections.length }}</b></div>
      <div class="row"><span class="muted">页面</span><span class="grow" />
        <b>{{ importResult.pages }}</b></div>
      <div class="row"><span class="muted">图片</span><span class="grow" />
        <b>{{ importResult.images }}</b></div>
      <div v-if="importResult.sections.length" class="import-tags">
        <n-tag v-for="s in importResult.sections" :key="s.id" size="small" :bordered="false">
          {{ s.name }} · {{ s.pages }} 页
        </n-tag>
      </div>
      <div v-if="importResult.missing.length" class="import-warn">
        缺图 {{ importResult.missing.length }} 张：{{ importResult.missing.slice(0, 5).join('、') }}
      </div>
      <div v-if="importResult.errors.length" class="import-warn">
        <div v-for="e in importResult.errors" :key="e.file">{{ e.file }}：{{ e.error }}</div>
      </div>
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button type="primary" @click="showImportResult = false">知道了</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
/* 整页不再滚动：记事本撑满内容区，左右两栏各自滚动 */
.notes-layout { display: flex; flex-direction: column; gap: 8px; height: 100%; min-height: 0; }

/* 顶部固定条：笔记本 + 分区页签 + 导入 / 新建分区 */
.notes-topbar { flex: 0 0 auto; display: flex; align-items: center; gap: 7px; min-width: 0; }
.tabs {
  flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 4px;
  overflow: hidden;                       /* 放不下的由「更多」接走，页签栏不滚动 */
}
.tab {
  flex: 0 0 auto; display: inline-flex; align-items: center; gap: 6px;
  height: 26px; padding: 0 10px; font: inherit; font-size: 12px;
  color: var(--ink-2); background: transparent;
  border: 1px solid transparent; border-radius: var(--radius-sm);
  cursor: pointer; white-space: nowrap;
}
.tab:hover { background: var(--paper-2); color: var(--ink); }
.tab.active {
  background: var(--surface); color: var(--accent-strong); font-weight: 600;
  border-color: var(--line); box-shadow: var(--shadow);
}
.tab-name { display: block; max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
.tab-more { color: var(--ink-3); border-color: var(--line-soft); }
.tab-rename { width: 96px; padding: 1px 4px; }

/* 主体：左侧页面列表 + 右侧编辑器，两栏独立滚动 */
.notes-body { flex: 1 1 auto; min-height: 0; display: flex; gap: 12px; }
.pages {
  width: 212px; flex: 0 0 212px; display: flex; flex-direction: column;
  min-height: 0; overflow: hidden; padding: 0;
}
.pages-head { flex: 0 0 auto; padding: 8px 10px 6px 12px; border-bottom: 1px solid var(--line-soft); }
.page-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 6px 8px 10px; }
.page-empty { padding: 8px 4px; }
.editor-pane { flex: 1 1 auto; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.editor-empty { flex: 1; display: flex; align-items: center; justify-content: center; }

/* 隐藏测量层：不占位不可见，只为拿到每个页签的自然宽度 */
.tab-measure {
  position: fixed; left: -10000px; top: 0; display: flex; gap: 4px;
  visibility: hidden; pointer-events: none;
}

.rename-input {
  font: inherit; font-size: 12px; min-width: 0;
  padding: 2px 4px; color: var(--ink); background: var(--surface);
  border: 1px solid var(--info-deep); border-radius: var(--radius-sm); outline: none;
}
.note-row { display: flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: var(--radius-sm); cursor: pointer; font-size: 12px; }
.import-progress {
  display: flex; gap: 3px; align-items: center; overflow: hidden;
  font-size: 12px; color: var(--info-deep);
}
.import-result { display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.import-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
.import-warn { margin-top: 6px; font-size: 12px; line-height: 1.65; color: var(--danger); }
.note-row:hover { background: var(--paper-2); }
.note-row.active { background: var(--accent-soft); color: var(--accent-strong); font-weight: 500; }
/* 拖拽排序的落点提示：分区页签与页面行共用 */
.tab.drop-target, .note-row.drop-target { outline: 2px dashed var(--accent); outline-offset: 1px; }
.toolbar { margin: 10px 0; gap: 5px; flex-wrap: wrap; }
.image-btn { font-size: 12px; color: var(--info-deep); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 2px 8px; cursor: pointer; }
.tiptap { flex: 1 1 auto; min-height: 0; border: 1px solid var(--line); background: var(--surface); border-radius: var(--radius-sm); padding: 20px 24px; overflow-y: auto; }
.tiptap :deep(.tiptap-body) { outline: none; min-height: 300px; font-size: 16px; line-height: 1.8; }
/* 阅读版心：正文与表格统一 720 居中；图片是唯一允许变宽的元素（见下一条） */
.tiptap :deep(.tiptap-body) > :not(img) { max-width: 720px; margin-inline: auto; }
.tiptap :deep(h1), .tiptap :deep(h2), .tiptap :deep(h3) { font-weight: 600; margin: 20px 0 8px; }
.tiptap :deep(ul), .tiptap :deep(ol) { padding-left: 22px; }
.tiptap :deep(pre) { background: var(--paper-2); padding: 10px; border-radius: var(--radius-sm); font-size: 13px; }
/* 粘贴图最长边可达 2560，允许出血到栏宽；height:auto 防止带 width/height 的旧笔记被压扁 */
.tiptap :deep(.tiptap-body) > img { display: block; margin-inline: auto; max-width: 100%; height: auto; border-radius: var(--r-xs); }
/* TipTap 在可编辑模式下总会套一层 tableWrapper，宽表在这里横向滚动而不撑破版面 */
.tiptap :deep(.tableWrapper) { overflow-x: auto; }
/* 表格：边框与底纹全部走主题 token，不引入新色值 */
.tiptap :deep(table) {
  border-collapse: collapse;
  table-layout: fixed;
  width: 100%;
  margin: 10px 0;
}
.tiptap :deep(th), .tiptap :deep(td) {
  position: relative;
  min-width: 40px;
  padding: 5px 8px;
  border: 1px solid var(--line);
  vertical-align: top;
  font-size: 12px;
  word-break: break-word;
}
.tiptap :deep(th) { background: var(--paper-3); font-weight: 600; text-align: left; }
/* 行高（整表）：由 table 节点的 rowHeight 属性渲染成 --row-h；未设置时回到自然高度 */
.tiptap :deep(table td), .tiptap :deep(table th) { height: var(--row-h, auto); }
/* 列宽拖拽手柄（Tiptap table resizable）：贴在列边界上的细条 + 拖拽光标 */
.tiptap :deep(.column-resize-handle) {
  position: absolute;
  right: -2px;
  top: 0;
  bottom: 0;
  width: 4px;
  background: var(--accent);
  opacity: .55;
  pointer-events: none;
}
.tiptap :deep(.resize-cursor) { cursor: col-resize; }
.tiptap :deep(th p), .tiptap :deep(td p) { margin: 0; }
/* 多单元格框选高亮（prosemirror-tables 的 selectedCell） */
.tiptap :deep(.selectedCell)::after {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--accent-soft);
  opacity: 0.55;
  pointer-events: none;
}
.tiptap :deep(.ProseMirror-selectednode) {
  outline: 2px solid var(--info-deep);
  outline-offset: 2px;
  border-radius: 4px;
}
</style>
