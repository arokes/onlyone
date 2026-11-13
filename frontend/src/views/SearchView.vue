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
import { api } from '@/api/client'
import { useJumpStore } from '@/stores/jump'
import { useSearchStore, type SearchItem } from '@/stores/search'

type Item = SearchItem
interface Preview {
  title: string
  text: string
  tables: string[][][]
  error: string
  source: string
  abs_path: string | null
  extract_status: string | null
}
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
  link: [
    'M10 14 14 10',
    'M8.6 12.4 6.2 14.8a3.4 3.4 0 0 0 4.8 4.8l2.4-2.4',
    'M15.4 11.6 17.8 9.2a3.4 3.4 0 0 0-4.8-4.8L10.6 6.8',
  ],
}

const message = useMessage()
const jump = useJumpStore()
const search = useSearchStore()
// 检索状态放在 store：页签切换会销毁本组件，状态不能留在组件内
const { q, items, semanticItems, pluginItems, mode, searched } = storeToRefs(search)
const busy = ref(false)

const showPreview = ref(false)
const preview = ref<Preview | null>(null)
const previewBusy = ref(false)

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
  if (!q.value.trim()) {
    search.clear()
    return
  }
  busy.value = true
  searched.value = true
  try {
    const r = await api<SearchResult>(
      '/api/search?q=' + encodeURIComponent(q.value.trim()),
    )
    search.setResult(r)
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
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

async function openPreviewById(entryId: number) {
  showPreview.value = true
  previewBusy.value = true
  preview.value = null
  try {
    preview.value = await api<Preview>('/api/preview?entry_id=' + entryId)
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
  if (it.type !== 'file') return '业务记录'
  return it.root_kind === 'anyshare_sync' ? '爱数同步盘' : '本地'
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
    q.value = query
    run()
  }
}, { immediate: true })

onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('onlyone:focus-search', focusSearch)
  loadProjects()
  // 跨重启只留关键词：有词且本次会话尚未检索过时，自动重跑一次。
  // URL 深链接 ?q= 走 pendingQuery 的 immediate watch，会先把 searched 置真，避免重复请求。
  if (q.value.trim() && !searched.value) void run()
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('onlyone:focus-search', focusSearch)
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
    <div v-if="searched" class="result-bar">
      <span class="metric"><b>{{ items.length }}</b> 字面命中</span>
      <span class="metric"><b>{{ semanticItems.length }}</b> 语义相近</span>
      <span class="metric"><b>{{ pluginItems.length }}</b> 外部索引</span>
      <span class="mode">{{ mode === 'fts5_trigram' ? 'FTS5 trigram' : 'LIKE 兜底（短词）' }}</span>
    </div>
  </div>

  <div v-if="!allGroups.length" class="card" style="margin-top: 14px">
    <div class="muted">
      {{ searched ? '没有匹配结果。' : '先添加一个索引根目录并扫描，或录入项目 / 日志 / 运维 / 链接，然后在这里搜索。' }}
    </div>
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

  <n-drawer v-model:show="showPreview" :width="640" placement="right">
    <n-drawer-content :title="preview?.title ?? '只读预览'" :native-scrollbar="false" closable>
      <n-spin v-if="previewBusy" size="small" />
      <template v-else-if="preview">
        <div class="muted" style="margin-bottom: 10px">
          来源：{{ preview.source }} · 抽取状态：{{ preview.extract_status || '—' }}
        </div>
        <n-alert v-if="preview.error" type="warning" style="margin-bottom: 12px">
          正文抽取失败：{{ preview.error }}
        </n-alert>
        <pre class="pv-text">{{ preview.text || '（无正文）' }}</pre>
        <div v-for="(t, ti) in preview.tables" :key="ti" class="pv-wrap">
          <div class="muted" style="margin: 12px 0 6px">表格 {{ ti + 1 }}</div>
          <table class="pv-table">
            <tr v-for="(row, ri) in t" :key="ri">
              <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
            </tr>
          </table>
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
.result-bar {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line-soft);
  font-size: 12px; color: var(--ink-3);
}
.result-bar .metric b { font-size: 14px; font-weight: 500; color: var(--ink); margin-right: 3px; }
.result-bar .mode {
  margin-left: auto; font-size: 11px; letter-spacing: 0.04em;
  color: var(--info-deep); background: var(--info-soft);
  border-radius: 4px; padding: 2px 7px;
}
.group-title { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
.group-label { font-size: 14px; font-weight: 500; color: var(--ink); }
.hit { padding: 12px 0; border-top: 1px solid var(--line-soft); }
.hit:first-of-type { border-top: none; }
.hit-head { display: flex; align-items: center; gap: 8px; }
.hit-badge {
  flex: 0 0 auto; font-size: 11px; line-height: 18px; padding: 0 7px;
  border-radius: 4px; background: var(--paper-2); color: var(--ink-2);
}
.hit-badge.is-title { background: var(--info-soft); color: var(--info-deep); }
.hit-title {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 14px; font-weight: 500; color: var(--ink);
  background: var(--info-soft); border-radius: 4px; padding: 4px 9px;
}
.hit-source { flex: 0 0 auto; font-size: 11px; color: var(--ink-3); }
.snippet-row { display: flex; align-items: flex-start; gap: 8px; margin-top: 7px; }
.snippet {
  flex: 1; min-width: 0; border-left: 2px solid var(--line); padding-left: 10px;
  font-size: 12px; color: var(--ink-2); line-height: 1.65;
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; word-break: break-word;
}
.snippet :deep(mark) { font-weight: 500; }
.hit-count { flex: 0 0 auto; font-size: 11px; color: var(--ink-3); padding-top: 2px; }
.hit-foot { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.path {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 11px; color: var(--ink-3);
}
.actions { flex: 0 0 auto; display: flex; gap: 5px; }
.icon-btn {
  width: 28px; height: 28px; flex: 0 0 auto; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--line); border-radius: 6px;
  background: var(--info-soft); color: var(--info-deep); cursor: pointer;
}
.icon-btn:hover:not(:disabled) { border-color: var(--info); color: var(--accent-strong); }
.icon-btn:disabled { opacity: 0.38; cursor: not-allowed; }
.icon-btn svg { width: 16px; height: 16px; }
.pv-text {
  white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.7;
  font-family: inherit; margin: 0; padding: 12px; background: var(--paper-2);
  border-radius: var(--radius-sm);
}
.pv-wrap { overflow-x: auto; }
.pv-table { border-collapse: collapse; font-size: 12px; }
.pv-table td { border: 1px solid var(--line); padding: 5px 9px; }
</style>
