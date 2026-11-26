<script setup lang="ts">
/**
 * 快速捕获浮层（2026-11-25 / 11-26）。
 *
 * 两种形态：
 * * standalone —— 独立无边框小窗（`?capture=1`），整页只有这张卡片，窗口由后端 show/hide；
 * * 主窗口浮层 —— 兼容旧形态，热键只走独立小窗，不再主动使用。
 *
 * 笔记模式用 TipTap：支持直接 Ctrl+V 粘贴截图。图片先以 base64 存在文档里，
 * 保存时后端 `persist_inline_images` 落盘并改写 src（此时才有 note_id）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { NButton, NInput, NSelect, useMessage } from 'naive-ui'
import type { SelectGroupOption } from 'naive-ui'
import { EditorContent, useEditor } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import TiptapImage from '@tiptap/extension-image'
import { api } from '@/api/client'

interface TargetResp {
  section_id: number | null
  notebook_name: string
  section_name: string
  notebooks: { id: number; name: string; sections: { id: number; name: string }[] }[]
}

/** standalone：独立小窗模式（?capture=1）。 */
const props = defineProps<{ standalone?: boolean }>()

const message = useMessage()
const show = ref(!!props.standalone)
const text = ref('')
const kind = ref<'incident' | 'note'>('incident')
const busy = ref(false)
const fore = ref<number | null>(null)
const inputEl = ref<InstanceType<typeof NInput> | null>(null)
const target = ref<TargetResp | null>(null)
const hint = ref('')
/** 粘贴过的图片（data URL）。TipTap 在个别交互后会丢掉 image 节点，
 *  这里留一份底稿，保存时若文档里没有 image 就补回去，保证截图不丢。 */
const pastedImages = ref<string[]>([])
let hintTimer: number | null = null
let timer: number | null = null

const MAX_IMAGE = 10 * 1024 * 1024

const noteEditor = useEditor({
  extensions: [
    StarterKit,
    // allowBase64：粘贴的截图先以 data URL 存在文档里，保存时才由后端落盘
    TiptapImage.configure({ allowBase64: true }),
  ],
  content: '',
  editorProps: {
    attributes: { class: 'cap-editor-body' },
    handlePaste: (_view, event) => handlePasteImage(event as ClipboardEvent),
  },
})

const targetOptions = computed<SelectGroupOption[]>(() =>
  (target.value?.notebooks ?? [])
    .filter(book => book.sections.length)
    .map(book => ({
      type: 'group' as const,
      label: book.name,
      key: 'book-' + book.id,
      children: book.sections.map(section => ({ label: section.name, value: section.id })),
    })),
)

const targetLabel = computed(() => {
  const t = target.value
  if (!t || !t.section_id) return '未设置分区'
  return `${t.notebook_name} / ${t.section_name}`
})

function flash(note: string) {
  hint.value = note
  if (hintTimer) window.clearTimeout(hintTimer)
  hintTimer = window.setTimeout(() => { hint.value = '' }, 2400)
}

async function loadTarget() {
  try {
    target.value = await api<TargetResp>('/api/capture/target')
  } catch {
    target.value = null
  }
}

/** 选中即保存为默认，下次直接用；不要求用户再进设置 */
async function pickTarget(sectionId: number) {
  if (!sectionId) return
  try {
    const saved = await api<{ section_name: string; notebook_name: string }>(
      '/api/capture/target', { method: 'PUT', body: JSON.stringify({ section_id: sectionId }) })
    if (target.value) {
      target.value.section_id = sectionId
      target.value.section_name = saved.section_name
      target.value.notebook_name = saved.notebook_name
    }
    flash(`已记住：${saved.notebook_name} / ${saved.section_name}`)
  } catch (e) {
    message.error(String(e))
  }
}

/** 粘贴截图：读成 data URL 后插入图片节点（不压缩，超过 10 MB 直接拦下） */
function handlePasteImage(event: ClipboardEvent): boolean {
  const items = Array.from(event.clipboardData?.items ?? [])
  const files = items
    .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
    .map(item => item.getAsFile())
    .filter((file): file is File => !!file)
  if (!files.length) return false
  event.preventDefault()
  for (const file of files) {
    if (file.size > MAX_IMAGE) {
      message.warning('单张图片不能超过 10 MB')
      continue
    }
    const reader = new FileReader()
    reader.onload = () => {
      const src = String(reader.result || '')
      const editor = noteEditor.value
      if (!src || !editor) return
      // setImage 在段落内插入块级节点会失败（焦点在行内），显式追加到文档末尾
      pastedImages.value.push(src)
      const node = editor.schema.nodes.image.create({ src })
      const at = editor.state.doc.content.size
      editor.chain().focus().insertContentAt(at, node).run()
    }
    reader.readAsDataURL(file)
  }
  return true
}

function noteJson() {
  return noteEditor.value?.getJSON() ?? { type: 'doc', content: [] }
}

function noteHasImage(): boolean {
  return JSON.stringify(noteJson()).includes('"type":"image"')
}

async function poll() {
  if (show.value) return
  try {
    const r = await api<{ open: boolean; fore: number | null }>('/api/capture/pending')
    if (!r.open) return
    fore.value = r.fore ?? null
    text.value = ''
    show.value = true
    void loadTarget()
    await nextTick()
    focusActive()
  } catch {
    // 后端未就绪/热键不可用：静默跳过，下一次继续
  }
}

function focusActive() {
  if (kind.value === 'note') noteEditor.value?.commands.focus()
  else inputEl.value?.focus()
}

function resetContent() {
  text.value = ''
  noteEditor.value?.commands.clearContent(true)
  pastedImages.value = []
  hint.value = ''
}

async function submit() {
  const plain = (noteEditor.value?.getText() ?? '').trim()
  const value = text.value.trim()
  if (kind.value === 'incident' && !value) {
    await close()
    return
  }
  if (kind.value === 'note' && !plain && !noteHasImage()) {
    message.warning('内容不能为空')
    return
  }
  busy.value = true
  try {
    const body: Record<string, unknown> = { kind: kind.value, fore: fore.value }
    if (kind.value === 'note') {
      const doc = noteJson() as { type?: string; content?: unknown[] }
      // 兜底：文档里若没留下 image 节点，把粘贴时缓存的图补在末尾
      if (pastedImages.value.length && !JSON.stringify(doc).includes('"type":"image"')) {
        doc.content = doc.content ?? []
        for (const src of pastedImages.value) {
          doc.content.push({ type: 'image', attrs: { src } })
        }
      }
      body.content_json = JSON.stringify(doc)
      body.title = (plain || '截图').slice(0, 40)
      body.text = plain || '截图'
      body.section_id = target.value?.section_id ?? null
    } else {
      body.text = value
    }
    await api('/api/capture', { method: 'POST', body: JSON.stringify(body) })
    if (props.standalone) {
      // 窗口由后端 hide；组件不卸载，清空内容即可，避免下次 show 出来是旧内容
      resetContent()
      return
    }
    show.value = false
    message.success(kind.value === 'note' ? '已存为笔记' : '已记入运维记录')
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

async function close() {
  if (props.standalone) resetContent()
  else show.value = false
  try {
    await api('/api/capture/dismiss', {
      method: 'POST',
      body: JSON.stringify({ fore: fore.value }),
    })
  } catch {
    // 焦点还不回去不影响使用
  }
}

function onKey(event: KeyboardEvent) {
  if (!show.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    void close()
  } else if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    void submit()
  }
}

/** 窗口从隐藏状态再次显示时，WebView 会重新拿到焦点，但输入区不一定聚焦 */
function onWindowFocus() {
  if (!show.value) return
  void nextTick(focusActive)
}

function onMaskClick() {
  if (!props.standalone) void close()
}

// 浮层显示期间隐藏整个工作台（仅主窗口形态使用；独立小窗没有工作台）
watch(show, visible => {
  if (props.standalone) return
  document.body.classList.toggle('capture-mode', visible)
})

watch(kind, () => { void nextTick(focusActive) })

onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('focus', onWindowFocus)
  if (props.standalone) {
    // 调试钩子：自动化测试用来读取编辑器真实状态（生产无副作用）
    ;(window as any).__capJson = () => JSON.stringify(noteJson())
    ;(window as any).__capHtml = () => noteEditor.value?.getHTML() ?? ''
    ;(window as any).__capSchema = () =>
      Object.keys((noteEditor.value?.schema.nodes ?? {}) as Record<string, unknown>)
    void loadTarget()
    void nextTick(focusActive)
    return
  }
  timer = window.setInterval(poll, 800)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('focus', onWindowFocus)
  if (timer) window.clearInterval(timer)
  if (hintTimer) window.clearTimeout(hintTimer)
  document.body.classList.remove('capture-mode')
})
</script>

<template>
  <Teleport to="body">
    <div v-if="show" class="cap-mask" :class="{ standalone }" @click.self="onMaskClick">
      <div class="cap-card">
        <div class="cap-head">
          <span
            class="cap-tab" :class="{ active: kind === 'incident' }"
            @click="kind = 'incident'"
          >运维</span>
          <span
            class="cap-tab" :class="{ active: kind === 'note' }"
            @click="kind = 'note'"
          >笔记</span>
          <span class="cap-hint">Ctrl+Alt+Space</span>
        </div>

        <div v-show="kind === 'incident'">
          <n-input
            ref="inputEl" v-model:value="text" type="textarea"
            :autosize="{ minRows: 3, maxRows: 4 }"
            placeholder="记一条……（Ctrl+Enter 保存，Esc 关闭）"
          />
        </div>

        <div v-show="kind === 'note'" class="cap-editor">
          <editor-content :editor="noteEditor" />
        </div>

        <div v-if="kind === 'note'" class="cap-target">
          <span class="cap-target-label">存到</span>
          <n-select
            size="tiny" :value="target?.section_id ?? null" :options="targetOptions"
            :placeholder="targetLabel" style="width: 240px"
            @update:value="pickTarget"
          />
          <span class="cap-target-hint">Ctrl+V 可粘贴截图</span>
        </div>

        <div class="cap-foot">
          <span class="cap-note">
            {{ hint || (kind === 'incident'
              ? '类型默认「新需求」，可稍后在运维页补全'
              : '选一次分区即记忆，Esc 取消') }}
          </span>
          <n-button size="small" type="primary" :loading="busy" @click="submit">保存</n-button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cap-mask {
  position: fixed; inset: 0;
  /* 低于 naive 的弹出层（默认 2000+），否则「存到」下拉会被遮罩盖在下面 */
  z-index: 1000;
  background: var(--paper);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 10px;
}
/* 独立小窗：卡片铺满 620×272 的无边框窗口 */
.cap-mask.standalone {
  background: var(--paper);
  padding: 8px;
  align-items: stretch;
}
.cap-mask.standalone .cap-card {
  width: 100%; max-width: none; height: 100%;
  box-shadow: none; overflow: auto;
}
.cap-card {
  width: 580px; max-width: calc(100vw - 20px);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 12px;
}
.cap-head { display: flex; align-items: center; gap: 4px; margin-bottom: 8px; }
.cap-tab {
  font-size: 12px; padding: 3px 10px; border-radius: 6px;
  color: var(--ink-2); cursor: pointer;
}
.cap-tab:hover { background: var(--paper-2); }
.cap-tab.active { background: var(--paper-2); color: var(--ink); }
.cap-hint { margin-left: auto; font-size: 11px; color: var(--ink-3); }

.cap-editor {
  height: 92px; overflow: auto; padding: 6px 8px;
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  background: var(--surface);
}
.cap-editor :deep(.cap-editor-body) { outline: none; font-size: 13px; line-height: 1.7; min-height: 60px; }
.cap-editor :deep(.cap-editor-body p) { margin: 0 0 4px; }
.cap-editor :deep(img) { max-width: 100%; border-radius: 4px; display: block; margin: 4px 0; }

.cap-target { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.cap-target-label { font-size: 11px; color: var(--ink-3); }
.cap-target-hint { font-size: 11px; color: var(--ink-3); }
.cap-foot { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.cap-note { flex: 1; min-width: 0; font-size: 11px; color: var(--ink-3); }
</style>
