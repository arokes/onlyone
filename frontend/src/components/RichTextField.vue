<script setup lang="ts">
/**
 * 业务记录里的富文本字段（现象 / 根因 / 处置 / 备注）。
 *
 * 与记事本同构：TipTap + ProseMirror JSON，不另做一套存储形态。
 * 不放工具栏按钮——只保留快捷键与 Ctrl+V 贴图（见下方 hint），
 * 避免弹窗里塞一排按钮把表单挤变形。
 *
 * 粘贴图片：剪贴板给位图文件时接管，压缩 → 上传 → 在光标处插入 image 节点。
 * 上传是异步的，用串行队列保证多图按粘贴顺序排开（与 NotesView 同一策略）。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { EditorContent, useEditor } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import TiptapImage from '@tiptap/extension-image'
import { uploadImageFile } from '@/utils/image'
import { toEditorContent } from '@/utils/richtext'
import ImageLightbox from './ImageLightbox.vue'

const props = defineProps<{
  modelValue: unknown
  placeholder?: string
  minHeight?: number
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: string): void }>()

const busy = ref(0)
const errorText = ref('')
const empty = ref(true)
let syncing = false
let uploadChain: Promise<void> = Promise.resolve()

const editor = useEditor({
  extensions: [
    StarterKit,
    // allowBase64=false：图片一律走服务端附件 URL，避免 base64 撑爆 content_json
    TiptapImage.configure({ allowBase64: false, inline: false }),
  ],
  content: toEditorContent(props.modelValue),
  editorProps: {
    attributes: { class: 'rich-body' },
    handlePaste: (_view, event) => pasteImages(event),
  },
  onCreate: ({ editor: ed }) => { empty.value = ed.isEmpty },
  onUpdate: ({ editor: ed }) => {
    empty.value = ed.isEmpty
    if (syncing) return
    emit('update:modelValue', JSON.stringify(ed.getJSON()))
  },
})

const surfaceStyle = computed(() => ({ '--rich-min-h': `${props.minHeight ?? 92}px` }))
const hint = 'Ctrl+V 贴图 · 双击图片看原图 · Ctrl+B 加粗 · Ctrl+I 斜体 · Ctrl+Shift+8 列表'

/** 双击图片看原图。单击保持 TipTap 的「选中节点」语义（拖拽 / Delete 删除），
 *  弹窗宽 780px、图片被 max-width 压到约 640px，4K 截图里的字必须放大才能看。 */
const viewerSrc = ref('')
function onDblClick(e: MouseEvent) {
  const el = e.target as HTMLElement | null
  if (!el || el.tagName !== 'IMG') return
  const src = (el as HTMLImageElement).src
  if (!src) return
  e.preventDefault()          // 不要让双击顺带选中正文
  viewerSrc.value = src
}

/** 外部值变化（切换到另一条记录 / 表单重置）时同步进编辑器，且不回灌 update 事件。 */
watch(() => props.modelValue, value => {
  const ed = editor.value
  if (!ed) return
  const next = toEditorContent(value)
  if (JSON.stringify(next) === JSON.stringify(ed.getJSON())) return
  syncing = true
  ed.commands.setContent(next, { emitUpdate: false })
  syncing = false
  empty.value = ed.isEmpty
})

onBeforeUnmount(() => { editor.value?.destroy() })

function pasteImages(event: ClipboardEvent): boolean {
  const dt = event.clipboardData
  if (!dt) return false
  const files: File[] = []
  for (const item of Array.from(dt.items || [])) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) files.push(file)
    }
  }
  if (!files.length) return false          // 纯文本 / 图文混排：交给默认粘贴，不丢字
  const anchor = editor.value?.state.selection.from ?? 0
  event.preventDefault()
  errorText.value = ''
  files.forEach((file, offset) => enqueue(file, anchor + offset))
  return true
}

/** 串行队列：多图按粘贴顺序插入；同批用 anchor + 序号依次排开。 */
function enqueue(file: File, pos: number) {
  busy.value++
  uploadChain = uploadChain.then(async () => {
    try {
      const uploaded = await uploadImageFile(file)
      const ed = editor.value
      if (!ed) return
      const max = ed.state.doc.content.size
      ed.chain().focus()
        .insertContentAt(Math.min(pos, max), { type: 'image', attrs: { src: uploaded.url } })
        .run()
      errorText.value = ''
    } catch (e) {
      errorText.value = '图片插入失败：' + String((e as Error)?.message ?? e)
    } finally {
      busy.value = Math.max(0, busy.value - 1)
    }
  })
}
</script>

<template>
  <div class="rich-field" :class="{ 'is-empty': empty }" :style="surfaceStyle"
       @dblclick="onDblClick">
    <editor-content :editor="editor" class="rich-surface" />
    <span v-if="empty && placeholder" class="rich-placeholder">{{ placeholder }}</span>
    <div class="rich-foot">
      <span v-if="busy" class="rich-status">正在上传 {{ busy }} 张图片…</span>
      <span v-else-if="errorText" class="rich-status err">{{ errorText }}</span>
      <span v-else class="rich-hint">{{ hint }}</span>
    </div>
    <image-lightbox v-if="viewerSrc" :src="viewerSrc" @close="viewerSrc = ''" />
  </div>
</template>

<style scoped>
.rich-field {
  position: relative;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  transition: border-color 0.15s;
}
.rich-field:focus-within { border-color: var(--accent); }
.rich-surface { padding: 8px 10px 2px; }
.rich-placeholder {
  position: absolute; top: 9px; left: 11px;
  font-size: 13px; color: var(--ink-3); pointer-events: none;
}
.rich-foot {
  display: flex; align-items: center; gap: 8px;
  padding: 2px 10px 6px; font-size: 11px; color: var(--ink-3); min-height: 18px;
}
.rich-status.err { color: var(--danger); }
.rich-hint { opacity: 0.75; }

:deep(.rich-body) {
  outline: none;
  min-height: var(--rich-min-h, 92px);
  font-size: 13px;
  line-height: 1.7;
  color: var(--ink-1);
}
:deep(.rich-body p) { margin: 0 0 4px; }
:deep(.rich-body h1), :deep(.rich-body h2), :deep(.rich-body h3) {
  font-size: 15px; font-weight: 600; margin: 8px 0 4px;
}
:deep(.rich-body ul), :deep(.rich-body ol) { padding-left: 20px; margin: 4px 0; }
:deep(.rich-body blockquote) {
  margin: 4px 0; padding-left: 10px; border-left: 3px solid var(--line); color: var(--ink-2);
}
:deep(.rich-body pre) {
  background: var(--paper-2); padding: 8px; border-radius: var(--radius-sm);
  font-size: 12px; overflow-x: auto;
}
:deep(.rich-body code) {
  background: var(--paper-2); padding: 1px 4px; border-radius: 3px; font-size: 12px;
}
:deep(.rich-body img) {
  display: block; max-width: 100%; height: auto;
  margin: 6px 0; border-radius: var(--r-xs); border: 1px solid var(--line);
}
:deep(.rich-body img.ProseMirror-selectednode) { outline: 2px solid var(--accent); }
</style>
