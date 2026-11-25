<script setup lang="ts">
/**
 * 项目事项（v0.9）：左侧项目列表 + 右侧项目工作台。
 *
 * 用户 2026-11-17 决定：三个业务页签（企微链接 / 运维记录）+ 关联文件，
 * 不再用抽屉看关联条目。业务表本来就有 project_id；
 * 关联文件仍来自搜索页「关联到项目」形成的 entry_relations。
 *
 * 2026-11-23 决定：**移除工作日志页签**（工作日志整体停用，见 App.vue 导航）。
 *
 * 2026-11-17 15:23 用户决定：**移除记事本页签**（只删 UI，notes.project_id
 * 与 /api/projects/{id}/notes 保留，已有归属页在「记事本 → 项目笔记」里照旧可看）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  NButton, NInput, NModal, NSelect, NTabPane, NTabs, NTag, useDialog, useMessage,
} from 'naive-ui'
import BizCrud from '@/components/BizCrud.vue'
import EmptyState from '@/components/EmptyState.vue'
import { incidentCategoryOptions, incidentRowClass } from '@/components/incident-options'
import { api } from '@/api/client'
import { useJumpStore } from '@/stores/jump'
import type { Field } from '@/components/biz-types'

interface Project {
  id: number
  name: string
  status: string
  note: string
  created_at: string
  entry_id?: number | null
}
interface FileRow {
  entry_id: number
  title: string
  summary: string
  abs_path: string
  ext: string
  size: number
}
interface Workspace {
  project: Project
  counts: { links: number; incidents: number; files: number }
  previews: Record<string, unknown[]>
}

const message = useMessage()
const dialog = useDialog()
const jump = useJumpStore()

const linkFields: Field[] = [
  { key: 'title', label: '标题', width: 200 },
  { key: 'url', label: '链接', type: 'url' },
  { key: 'project_id', label: '归属项目', type: 'project', width: 150 },
  { key: 'note', label: '备注', type: 'textarea', hideInTable: true },
]
const incidentFields: Field[] = [
  { key: 'occurred_at', label: '发生时间', type: 'datetime', width: 160 },
  { key: 'category', label: '类型', type: 'select', width: 100, options: incidentCategoryOptions },
  { key: 'system', label: '系统', width: 140 },
  { key: 'proposer', label: '提出人', width: 96 },
  { key: 'company', label: '公司', width: 110 },
  { key: 'symptom', label: '现象', type: 'rich', width: 170 },
  { key: 'root_cause', label: '根因', type: 'rich', hideInTable: true },
  { key: 'action', label: '处置', type: 'rich', hideInTable: true },
  {
    key: 'status', label: '状态', type: 'select', width: 100,
    options: [{ label: '未关闭', value: 'open' }, { label: '已关闭', value: 'closed' }],
  },
  { key: 'duration_min', label: '耗时(分)', type: 'number', width: 100 },
  { key: 'project_id', label: '归属项目', type: 'project', width: 140 },
  { key: 'note', label: '备注', type: 'rich', hideInTable: true },
]

const projects = ref<Project[]>([])
const activeId = ref<number | null>(null)
const keyword = ref('')
const status = ref<'active' | 'archived' | 'all'>('active')
const TABS = ['links', 'incidents', 'files']
// ?ptab=files 直达某个页签（headless 截图与文档链接用）
const ptab = new URLSearchParams(window.location.search).get('ptab')
const tab = ref(ptab && TABS.includes(ptab) ? ptab : 'links')
const counts = reactive({ links: 0, incidents: 0, files: 0 })
const projectEntryId = ref<number | null>(null)
const files = ref<FileRow[]>([])
const loadingFiles = ref(false)

const active = computed(() => projects.value.find(p => p.id === activeId.value) ?? null)
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return projects.value.filter(p => {
    if (status.value !== 'all' && p.status !== status.value) return false
    return !kw || p.name.toLowerCase().includes(kw)
  })
})
const activeCount = computed(() => projects.value.filter(p => p.status === 'active').length)
const archivedCount = computed(() => projects.value.filter(p => p.status !== 'active').length)

async function loadProjects(keepActive = true) {
  try {
    const list = await api<Project[]>('/api/biz/projects')
    projects.value = list
    if (!keepActive || !list.some(p => p.id === activeId.value)) {
      const pool = list.filter(p => p.status === 'active')
      activeId.value = (pool[0] ?? list[0])?.id ?? null
    }
  } catch (e) {
    message.error(String(e))
  }
}

async function loadWorkspace() {
  if (!activeId.value) {
    Object.assign(counts, { links: 0, incidents: 0, files: 0 })
    projectEntryId.value = null
    return
  }
  try {
    const ws = await api<Workspace>(`/api/projects/${activeId.value}/workspace?limit=0`)
    Object.assign(counts, ws.counts)
    projectEntryId.value = ws.project.entry_id ?? null
  } catch (e) {
    message.error(String(e))
  }
}

async function loadFiles() {
  if (!activeId.value) {
    files.value = []
    return
  }
  loadingFiles.value = true
  try {
    files.value = await api<FileRow[]>(`/api/projects/${activeId.value}/files`)
  } catch (e) {
    message.error(String(e))
  } finally {
    loadingFiles.value = false
  }
}

function refresh() {
  loadWorkspace()
  if (tab.value === 'files') loadFiles()
}

watch(activeId, () => {
  files.value = []
  refresh()
})

watch(tab, value => {
  if (value === 'files' && !files.value.length) loadFiles()
})

onMounted(async () => {
  await loadProjects()
  await loadWorkspace()
})

// ---------------------------------------------------------------- 项目本身 CRUD

const showProject = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = reactive({ name: '', status: 'active', note: '' })

function openCreateProject() {
  editingId.value = null
  form.name = ''
  form.status = 'active'
  form.note = ''
  showProject.value = true
}

function openEditProject() {
  if (!active.value) return
  editingId.value = active.value.id
  form.name = active.value.name
  form.status = active.value.status
  form.note = active.value.note ?? ''
  showProject.value = true
}

async function saveProject() {
  const name = form.name.trim()
  if (!name) {
    message.warning('项目名称不能为空')
    return
  }
  saving.value = true
  try {
    const body = JSON.stringify({ data: { name, status: form.status, note: form.note } })
    if (editingId.value) {
      await api(`/api/biz/projects/${editingId.value}`, { method: 'PUT', body })
    } else {
      const r = await api<{ id: number }>('/api/biz/projects', { method: 'POST', body })
      await loadProjects(false)
      activeId.value = r.id
    }
    showProject.value = false
    message.success('已保存')
    await loadProjects()
    await loadWorkspace()
  } catch (e) {
    message.error(String(e))
  } finally {
    saving.value = false
  }
}

async function setStatus(next: string) {
  if (!active.value) return
  try {
    await api(`/api/biz/projects/${active.value.id}`, {
      method: 'PUT',
      body: JSON.stringify({ data: { status: next } }),
    })
    message.success(next === 'archived' ? '已归档' : '已恢复为进行中')
    await loadProjects()
    await loadWorkspace()
  } catch (e) {
    message.error(String(e))
  }
}

function removeProject() {
  if (!active.value) return
  const row = active.value
  dialog.warning({
    title: '确认删除项目',
    content: `删除「${row.name}」后，其检索条目与关联关系会一并移除；企微链接 / 日志 / 运维记录本身保留，但归属项目会清空。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api(`/api/biz/projects/${row.id}`, { method: 'DELETE' })
        message.success('已删除')
        activeId.value = null
        await loadProjects(false)
        await loadWorkspace()
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

// ---------------------------------------------------------------- 关联文件

async function openFile(row: FileRow) {
  try {
    await api('/api/open', { method: 'POST', body: JSON.stringify({ path: row.abs_path }) })
  } catch (e) {
    message.error(String(e))
  }
}

function gotoSearch(entryId: number, title?: string) {
  if (!entryId) return
  // 带上文件名：关掉预览后搜索页能看到这组结果，而不是空搜索页
  jump.jumpToEntry(entryId, title)
}

function fmtSize(size: number): string {
  if (!size) return '—'
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(size / 1024))} KB`
}

function shortTime(text: string): string {
  return (text || '').slice(5, 16)
}
</script>

<template>
  <div class="proj-layout">
    <aside class="card rail">
      <div class="row" style="margin-bottom: 10px">
        <span class="section-title">项目</span>
        <span class="count-chip">{{ projects.length }}</span>
        <div class="grow" />
        <n-button size="tiny" type="primary" @click="openCreateProject">新建</n-button>
      </div>

      <n-input v-model:value="keyword" size="small" clearable placeholder="搜索项目名称" />

      <div class="seg">
        <button type="button" :class="{ on: status === 'active' }" @click="status = 'active'">
          进行中 {{ activeCount }}
        </button>
        <button type="button" :class="{ on: status === 'archived' }" @click="status = 'archived'">
          已归档 {{ archivedCount }}
        </button>
        <button type="button" :class="{ on: status === 'all' }" @click="status = 'all'">
          全部
        </button>
      </div>

      <div class="list">
        <button
          v-for="p in filtered" :key="p.id" type="button"
          class="item" :class="{ on: p.id === activeId }"
          @click="activeId = p.id"
        >
          <span class="item-name ellipsis">{{ p.name }}</span>
          <span v-if="p.status !== 'active'" class="item-arch">归档</span>
        </button>
        <div v-if="!filtered.length" class="muted" style="padding: 10px 2px">
          没有匹配的项目。可以切换筛选或新建一个。
        </div>
      </div>
    </aside>

    <section v-if="active" class="pane">
      <div class="card" style="margin-bottom: 12px">
        <div class="row">
          <h3 class="section-title">{{ active.name }}</h3>
          <n-tag size="small" :bordered="false" :type="active.status === 'active' ? 'info' : 'default'">
            {{ active.status === 'active' ? '进行中' : '已归档' }}
          </n-tag>
          <span class="muted">关联文件 {{ counts.files }} · 创建于 {{ shortTime(active.created_at) }}</span>
          <div class="grow" />
          <n-button size="small" @click="openEditProject">编辑</n-button>
          <n-button size="small" @click="setStatus(active.status === 'active' ? 'archived' : 'active')">
            {{ active.status === 'active' ? '归档' : '恢复' }}
          </n-button>
          <n-button size="small" quaternary type="error" @click="removeProject">删除</n-button>
        </div>
        <div v-if="active.note" class="muted" style="margin-top: 6px">{{ active.note }}</div>
      </div>

      <n-tabs v-model:value="tab" type="line" display-directive="show:lazy" animated>
        <n-tab-pane name="links">
          <template #tab>
            <span>企微链接<i v-if="counts.links" class="badge">{{ counts.links }}</i></span>
          </template>
          <biz-crud kind="links" title="企微链接" :fields="linkFields" :project-id="activeId" />
        </n-tab-pane>

        <n-tab-pane name="incidents">
          <template #tab>
            <span>运维记录<i v-if="counts.incidents" class="badge">{{ counts.incidents }}</i></span>
          </template>
          <biz-crud kind="incidents" title="运维记录" :fields="incidentFields"
                    :project-id="activeId" :row-class="incidentRowClass" />
        </n-tab-pane>

        <n-tab-pane name="files">
          <template #tab>
            <span>关联文件<i v-if="counts.files" class="badge">{{ counts.files }}</i></span>
          </template>
          <div class="card">
            <div class="row" style="margin-bottom: 8px">
              <span class="section-title">关联文件</span>
              <span class="count-chip">{{ files.length }}</span>
              <div class="grow" />
              <n-button size="small" @click="loadFiles">刷新</n-button>
            </div>
            <div class="muted" style="margin-bottom: 8px">
              来自全局搜索结果的「关联到项目」，只登记关系，文件仍在原目录。
            </div>
            <div v-if="loadingFiles" class="muted">加载中…</div>
            <div v-else-if="!files.length" class="empty">
              还没有关联文件。到全局搜索里找到文件后点「关联到项目」即可。
            </div>
            <div v-for="f in files" :key="f.entry_id" class="file-row">
              <span class="ellipsis" style="flex: 1">{{ f.title }}</span>
              <span class="muted">{{ fmtSize(f.size) }}</span>
              <n-button size="tiny" secondary @click="openFile(f)">打开</n-button>
              <n-button size="tiny" quaternary @click="gotoSearch(f.entry_id, f.title)">查看</n-button>
            </div>
          </div>
        </n-tab-pane>
      </n-tabs>
    </section>

    <section v-else class="pane">
      <div class="card">
        <empty-state
          title="还没有项目"
          desc="项目是把企微链接、运维记录和关联文件串起来的那根线。建一个，之后就能按项目回看全部上下文。"
          action-text="新建项目"
          hint="也可以先在别处录入记录，之后再归到项目下"
          @action="openCreateProject"
        />
      </div>
    </section>

    <n-modal
      v-model:show="showProject" preset="card"
      :title="(editingId ? '编辑' : '新建') + '项目'" style="width: 520px"
    >
      <div class="form-row">
        <label class="form-label">项目名称<span class="req">*</span></label>
        <div class="grow"><n-input v-model:value="form.name" placeholder="例如：SAP 主数据治理" /></div>
      </div>
      <div class="form-row">
        <label class="form-label">状态</label>
        <div class="grow">
          <n-select v-model:value="form.status" :options="[
            { label: '进行中', value: 'active' }, { label: '已归档', value: 'archived' },
          ]" />
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">说明</label>
        <div class="grow">
          <n-input v-model:value="form.note" type="textarea" :rows="3" placeholder="可选" />
        </div>
      </div>
      <template #footer>
        <div class="row" style="justify-content: flex-end">
          <n-button @click="showProject = false">取消</n-button>
          <n-button type="primary" :loading="saving" @click="saveProject">保存</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.proj-layout { display: flex; gap: 14px; align-items: flex-start; }

.rail {
  width: 236px;
  flex-shrink: 0;
  position: sticky;
  top: 0;
  padding: 14px 12px;
}
.seg { display: flex; gap: 4px; margin: 8px 0; }
.seg button {
  flex: 1;
  padding: 3px 0;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--ink-2);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.seg button:hover { background: var(--paper-2); }
/* 值类语义：橙底 + 橙字，去掉橙色描边（描边是导航级的语汇，重复使用会让两套语义混淆） */
.seg button.on {
  background: var(--accent-soft);
  border-color: transparent;
  color: var(--accent-strong);
  font-weight: 500;
}
.list { max-height: calc(100vh - 230px); overflow-y: auto; }
.item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--ink-2);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.item:hover { background: var(--paper-2); color: var(--ink); }
.item.on { background: var(--accent-soft); color: var(--accent-strong); font-weight: 500; }
.item-name { flex: 1; }
.item-arch {
  flex-shrink: 0;
  font-size: 12px;
  padding: 0 4px;
  border-radius: var(--r-xs);
  background: var(--paper-3);
  color: var(--ink-2);
}

.pane { flex: 1; min-width: 0; }
.empty-pane { padding: 28px; color: var(--ink-3); line-height: 1.8; }

.badge {
  display: inline-block;
  margin-left: 5px;
  padding: 0 5px;
  border-radius: var(--r-xs);
  background: var(--paper-2);
  color: var(--ink-2);
  font-size: 12px;
  font-style: normal;
  line-height: 18px;
}

.file-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 2px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 13px;
}
.file-row:last-child { border-bottom: none; }

.empty { padding: 14px 2px; color: var(--ink-3); font-size: 12px; line-height: 1.7; }

.form-row { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.form-label { width: 78px; flex-shrink: 0; padding-top: 6px; font-size: 13px; color: var(--ink-2); }
.req { color: var(--danger); margin-left: 3px; }
</style>
