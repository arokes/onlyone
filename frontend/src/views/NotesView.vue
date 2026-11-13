<script setup lang="ts">
/**
 * 记事本（M7，B 档）：笔记本 → 分区 → 页面三级树 + TipTap 富文本 +
 * 防抖自动保存（revision 乐观锁）+ 图片粘贴 + 私密页 + 导出/全库备份。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorContent, useEditor } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import TiptapImage from '@tiptap/extension-image'
import {
  NButton, NInput, NModal, NSwitch, NTag, useDialog, useMessage,
} from 'naive-ui'
import { api, TOKEN } from '@/api/client'

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
const books = ref<Notebook[]>([])
const expanded = ref<Record<number, boolean>>({})
const activeId = ref<number | null>(null)
const detail = ref<NoteDetail | null>(null)
const title = ref('')
const isPrivate = ref(false)
const revision = ref(0)
const saveState = ref<'saved' | 'saving' | 'dirty'>('saved')
const busy = ref(false)

const showDialog = ref(false)
const dialogMode = ref<'book' | 'section'>('book')
const dialogTarget = ref<number | null>(null)
const dialogName = ref('')
const dialogAction = ref<'create' | 'rename'>('create')

let saveTimer: number | undefined
let loadingContent = false

const editor = useEditor({
  extensions: [StarterKit, TiptapImage],
  content: '',
  editorProps: { attributes: { class: 'tiptap-body' } },
  onUpdate: () => {
    if (!loadingContent) markDirty()
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

async function loadTree() {
  try {
    books.value = await api<Notebook[]>('/api/notebooks')
    for (const b of books.value) {
      if (expanded.value[b.id] === undefined) expanded.value[b.id] = true
    }
  } catch (e) {
    message.error(String(e))
  }
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

async function onPickImage(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !activeId.value) return
  try {
    const dataUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = reject
      reader.readAsDataURL(file)
    })
    const r = await api<{ url: string }>(`/api/notes/${activeId.value}/images`, {
      method: 'POST', body: JSON.stringify({ data: dataUrl, name: file.name }),
    })
    editor.value?.chain().focus().setImage({ src: r.url }).run()
  } catch (e) {
    message.error('图片插入失败：' + String(e))
  } finally {
    input.value = ''
  }
}

async function download(scope: 'page' | 'section' | 'notebook', format: 'md' | 'html' | 'zip') {
  let id = 0
  if (scope === 'page') id = activeId.value ?? 0
  else if (scope === 'section') id = activeNote.value?.section_id ?? 0
  else {
    const sectionId = activeNote.value?.section_id
    id = books.value.find(b => b.sections.some(s => s.id === sectionId))?.id ?? 0
  }
  if (!id) { message.warning('请先选择一个页面'); return }
  try {
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
    message.success(format === 'zip' ? '已导出 HTML + 图片 zip' : '已导出')
  } catch (e) {
    message.error('导出失败：' + String(e))
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
  // 支持 ?tab=notes&note=<id> 深链接（跨页跳转与 headless 截图用）
  const noteParam = new URLSearchParams(window.location.search).get('note')
  if (noteParam && /^\d+$/.test(noteParam)) await openNote(Number(noteParam))
})
onBeforeUnmount(() => {
  if (saveTimer) window.clearTimeout(saveTimer)
  editor.value?.destroy()
})
</script>

<template>
  <div class="notes-layout">
    <aside class="card tree">
      <div class="row" style="margin-bottom: 10px">
        <h3 style="font-size: 14px; font-weight: 500">笔记本</h3>
        <div class="grow" />
        <n-button size="tiny" secondary @click="openDialog('book')">+ 新建</n-button>
      </div>
      <div v-if="!books.length" class="muted">还没有笔记本。</div>
      <div v-for="b in books" :key="b.id" class="book">
        <div class="row book-head">
          <span class="grow ellipsis" style="cursor: pointer" @click="expanded[b.id] = !expanded[b.id]">
            {{ expanded[b.id] ? '▾' : '▸' }} {{ b.name }}
          </span>
          <n-button size="tiny" quaternary @click="openDialog('section', b.id)">+分区</n-button>
          <n-button size="tiny" quaternary @click="openDialog('book', b.id, b.name, 'rename')">改名</n-button>
          <n-button size="tiny" quaternary @click="confirmDelete('notebook', b.id, b.name)">删</n-button>
        </div>
        <div v-if="expanded[b.id]">
          <div v-for="s in b.sections" :key="s.id" class="section">
            <div class="row section-head">
              <span class="grow ellipsis muted">{{ s.name }}</span>
              <n-button size="tiny" quaternary @click="createNote(s.id)">+页面</n-button>
              <n-button size="tiny" quaternary @click="openDialog('section', s.id, s.name, 'rename')">改名</n-button>
              <n-button size="tiny" quaternary @click="confirmDelete('section', s.id, s.name)">删</n-button>
            </div>
            <div
              v-for="n in s.notes" :key="n.id" class="note-row"
              :class="{ active: activeId === n.id }" @click="openNote(n.id)"
            >
              <span class="grow ellipsis">{{ n.title || '未命名页面' }}</span>
              <n-tag v-if="n.is_private" size="tiny" :bordered="false">私密</n-tag>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <section class="card editor-pane">
      <div v-if="!detail" class="muted">从左侧选择一个页面，或先新建笔记本 / 分区 / 页面。</div>
      <template v-else>
        <div class="row">
          <n-input v-model:value="title" class="grow" placeholder="页面标题" />
          <span class="muted">不参与检索</span>
          <n-switch v-model:value="isPrivate" size="small" />
          <n-tag size="small" :bordered="false" :type="saveState === 'saved' ? 'success' : 'warning'">
            {{ saveState === 'saved' ? '已保存' : saveState === 'saving' ? '保存中' : '待保存' }}
          </n-tag>
          <n-button size="tiny" quaternary @click="confirmDelete('note', detail.id, title)">删除页面</n-button>
        </div>
        <div class="row toolbar">
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleBold().run()">B</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleItalic().run()">I</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleHeading({ level: 2 }).run()">H2</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleBulletList().run()">列表</n-button>
          <n-button size="tiny" secondary @click="editor?.chain().focus().toggleCodeBlock().run()">代码</n-button>
          <label class="image-btn">
            图片
            <input type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden @change="onPickImage">
          </label>
          <div class="grow" />
          <span class="muted">改动后 1 秒自动保存 · revision {{ revision }}</span>
        </div>
        <editor-content :editor="editor" class="tiptap" />
        <div class="row export-row">
          <n-button size="tiny" @click="download('page', 'md')">导出本页 MD</n-button>
          <n-button size="tiny" @click="download('page', 'zip')">导出本页 HTML+图片</n-button>
          <n-button size="tiny" @click="download('section', 'zip')">导出分区</n-button>
          <n-button size="tiny" @click="download('notebook', 'zip')">导出笔记本</n-button>
          <div class="grow" />
          <n-button size="tiny" type="primary" @click="backup">全库备份</n-button>
        </div>
      </template>
    </section>
  </div>

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
</template>

<style scoped>
.notes-layout { display: flex; gap: 14px; min-height: 560px; }
.tree { width: 290px; flex-shrink: 0; overflow-y: auto; max-height: calc(100vh - 140px); }
.editor-pane { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.book { margin-bottom: 8px; }
.book-head { padding: 4px 0; font-size: 13px; }
.section { margin-left: 12px; border-left: 2px solid var(--line-soft); padding-left: 8px; }
.section-head { padding: 3px 0; }
.note-row { display: flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: var(--radius-sm); cursor: pointer; font-size: 12px; }
.note-row:hover { background: var(--paper-2); }
.note-row.active { background: var(--accent-soft); color: var(--accent-strong); font-weight: 500; }
.toolbar { margin: 10px 0; gap: 5px; flex-wrap: wrap; }
.image-btn { font-size: 12px; color: var(--info-deep); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 2px 8px; cursor: pointer; }
.tiptap { flex: 1; min-height: 320px; border: 1px solid var(--line); background: var(--surface); border-radius: var(--radius-sm); padding: 12px 16px; overflow-y: auto; }
.tiptap :deep(.tiptap-body) { outline: none; min-height: 300px; line-height: 1.75; }
.tiptap :deep(h1), .tiptap :deep(h2), .tiptap :deep(h3) { font-weight: 600; margin: 12px 0 6px; }
.tiptap :deep(ul), .tiptap :deep(ol) { padding-left: 22px; }
.tiptap :deep(pre) { background: var(--paper-2); padding: 10px; border-radius: var(--radius-sm); }
.tiptap :deep(img) { max-width: 100%; border-radius: 4px; }
.export-row { margin-top: 10px; gap: 6px; flex-wrap: wrap; }
</style>
