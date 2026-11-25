<script setup lang="ts">
/**
 * 概览（2026-11-25）：默认落地页。只读聚合，所有点击都跳到对应页签去处理，
 * 不在本页开第二套编辑入口。
 *
 * 数据全部来自 `GET /api/overview`（见 backend/onlyone/overview.py）：
 * 超期判定已由后端算好（days_open / overdue），前端只负责显示，不重算，
 * 避免出现「概览说超期、运维页说不超期」两套口径。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { NButton, useDialog, useMessage } from 'naive-ui'
import EmptyState from '@/components/EmptyState.vue'
import { api } from '@/api/client'
import { useJumpStore } from '@/stores/jump'

interface Counts {
  roots: number; files: number; entries: number; notes: number
  projects: number; links: number
  incidents_open: number; incidents_overdue: number; failures: number
}
interface RecentFile {
  entry_id: number | null; title: string; rel_path: string; ext: string
  size: number; mtime: string; root_kind: string
}
interface OpenIncident {
  id: number; occurred_at: string; system: string; category: string
  symptom: string; title: string; days_open: number | null; overdue: boolean
}
interface RecentNote { id: number; title: string; section: string; updated_at: string }
interface Storage {
  db_mb: number; used_mb: number; free_mb: number; free_ratio: number
  backup_at: string | null; backup_days_ago: number | null
}
interface Scan { phase: string; percent: number; watched_roots: number; last_event_at: string | null }
interface Overview {
  generated_at: string; counts: Counts; recent_files: RecentFile[]
  open_incidents: OpenIncident[]; recent_notes: RecentNote[]
  storage: Storage | null; scan: Scan | null; warnings: string[]
}

const jump = useJumpStore()
const dialog = useDialog()
const message = useMessage()

const data = ref<Overview | null>(null)
const loading = ref(false)
const error = ref('')
const BACKUP_STALE_DAYS = 7

/** 扫描进行中时只轮询轻量的 /api/scan/status，不重拉整个概览 */
let scanTimer: number | null = null
let lastLoadedAt = 0

async function load() {
  loading.value = true
  try {
    data.value = await api<Overview>('/api/overview')
    error.value = ''
    lastLoadedAt = Date.now()
    syncScanTimer()
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

function syncScanTimer() {
  const scanning = data.value?.scan?.phase === 'scanning'
  if (scanning && scanTimer === null) {
    scanTimer = window.setInterval(refreshScan, 2000)
  } else if (!scanning && scanTimer !== null) {
    window.clearInterval(scanTimer)
    scanTimer = null
  }
}

async function refreshScan() {
  try {
    const st = await api<Scan>('/api/scan/status')
    if (data.value) {
      data.value.scan = { ...(data.value.scan ?? st), phase: st.phase, percent: st.percent }
    }
    syncScanTimer()
  } catch {
    if (scanTimer !== null) { window.clearInterval(scanTimer); scanTimer = null }
  }
}

/** 切回窗口时静默刷新，但 30 秒内不重复打库 */
function onVisible() {
  if (document.visibilityState === 'visible' && Date.now() - lastLoadedAt > 30000) load()
}

onMounted(() => {
  load()
  document.addEventListener('visibilitychange', onVisible)
})
onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisible)
  if (scanTimer !== null) window.clearInterval(scanTimer)
})

const todayText = computed(() => {
  const d = new Date()
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 周${week}`
})

const counts = computed<Counts | null>(() => data.value?.counts ?? null)

/** 一句话摘要：只说最该知道的那件事 */
const summaryText = computed(() => {
  const c = counts.value
  if (!c) return ''
  const items = data.value?.open_incidents ?? []
  if (!c.incidents_open) return '今天没有待跟进的运维记录。'
  if (!c.incidents_overdue) return `${c.incidents_open} 条运维未关闭，暂无超期。`
  const worst = items.reduce((max, it) => Math.max(max, it.days_open ?? 0), 0)
  return `${c.incidents_open} 条运维未关闭，其中 ${c.incidents_overdue} 条超期（最久 ${worst} 天）。`
})

const statTiles = computed(() => {
  const c = counts.value
  if (!c) return []
  return [
    { label: '索引文件', value: c.files, tone: '', tab: 'roots' },
    { label: '全库条目', value: c.entries, tone: '', tab: '' },
    { label: '未关闭运维', value: c.incidents_open, tone: c.incidents_open ? 'accent' : '', tab: 'incidents' },
    { label: '超期未处理', value: c.incidents_overdue, tone: c.incidents_overdue ? 'warn' : '', tab: 'incidents' },
    { label: '抽取失败', value: c.failures, tone: c.failures ? 'warn' : 'ok', tab: 'roots' },
  ]
})

const backupStale = computed(() => {
  const days = data.value?.storage?.backup_days_ago
  return days === null || days === undefined || days > BACKUP_STALE_DAYS
})

const scanText = computed(() => {
  const s = data.value?.scan
  if (!s) return '扫描状态不可用'
  if (s.phase === 'scanning') return `正在扫描 ${s.percent.toFixed(0)}%`
  const when = s.last_event_at ? formatWhen(s.last_event_at) : '无记录'
  return `空闲 · 最近事件 ${when}`
})

function go(tab: string) {
  if (tab) jump.activeTab = tab
}

/** 文件名 → 查询词：去掉扩展名。title 就是文件名，`.docx` 这类后缀在正文里没有，
 *  去掉后短语更短、命中更稳（实测完整名也能命中，但去后缀对 0 命中的场景更友好）。 */
function fileQuery(row: RecentFile): string {
  const title = String(row.title || '').trim()
  return title.replace(/\.[A-Za-z0-9]{1,5}$/, '').trim() || title
}

function openFile(row: RecentFile) {
  if (row.entry_id == null) {
    message.warning('该文件已不在索引中，可在「索引目录」里重建索引后重试')
    return
  }
  // 带文件名一起跳：搜索页会以它为关键词检索，关掉预览后能看到结果列表而不是空页面
  jump.jumpToEntry(row.entry_id, fileQuery(row))
}

function formatWhen(raw: string | null | undefined): string {
  if (!raw) return '—'
  const t = Date.parse(String(raw).replace(' ', 'T'))
  if (Number.isNaN(t)) return String(raw)
  const diff = Date.now() - t
  const day = 86400000
  if (diff < 60000) return '刚刚'
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  const d = new Date(t)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const sameDay = new Date().toDateString() === d.toDateString()
  if (sameDay) return `今天 ${hm}`
  if (diff < 2 * day) return `昨天 ${hm}`
  if (diff < 7 * day) return `${Math.floor(diff / day)} 天前`
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${hm}`
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1048576).toFixed(1)} MB`
}

function backupNow() {
  dialog.warning({
    title: '立即备份',
    content: '将把当前数据库复制到 data/backups（含凭据密文与索引明文），确认继续？',
    positiveText: '备份',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        const r = await api<{ file: string }>('/api/backup', { method: 'POST' })
        message.success(`已备份：${r.file}`)
        await load()
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}
</script>

<template>
  <div class="home">
    <div class="row head">
      <h2 class="section-title">概览</h2>
      <span class="muted">{{ todayText }}</span>
      <div class="grow" />
      <span class="muted" :class="{ live: data?.scan?.phase === 'scanning' }">{{ scanText }}</span>
      <n-button size="small" quaternary :loading="loading" @click="load">刷新</n-button>
    </div>

    <p v-if="error" class="summary error">概览加载失败：{{ error }}</p>
    <p v-else-if="data" class="summary">{{ summaryText }}</p>

    <div v-if="counts" class="stat-strip">
      <div v-for="t in statTiles" :key="t.label" class="stat-tile"
           :class="{ clickable: !!t.tab }" @click="go(t.tab)">
        <div class="stat-label">{{ t.label }}</div>
        <div class="stat-value" :class="t.tone">{{ t.value }}</div>
      </div>
    </div>

    <div class="grid">
      <section class="card">
        <div class="card-head">
          <h3 class="section-title">最近改动</h3>
          <span class="count-chip">{{ data?.recent_files.length ?? 0 }}</span>
        </div>
        <template v-if="data?.recent_files.length">
          <div v-for="f in data.recent_files" :key="f.rel_path + f.mtime" class="home-row"
               role="button" tabindex="0"
               :title="f.rel_path"
               @click="openFile(f)" @keydown.enter="openFile(f)">
            <span class="ext">{{ (f.ext || '?').toUpperCase() }}</span>
            <span class="cell-main">{{ f.title }}</span>
            <span class="cell-meta">{{ formatSize(f.size) }}</span>
            <span class="cell-time">{{ formatWhen(f.mtime) }}</span>
          </div>
        </template>
        <empty-state
          v-else compact title="还没有索引到文件"
          desc="添加一个索引目录后，最近的文档会出现在这里。"
          action-text="添加索引目录" @action="go('roots')"
        />
      </section>

      <section class="card">
        <div class="card-head">
          <h3 class="section-title">待跟进运维</h3>
          <span class="count-chip">{{ data?.counts.incidents_open ?? 0 }}</span>
        </div>
        <template v-if="data?.open_incidents.length">
          <div v-for="it in data.open_incidents" :key="it.id"
               class="home-row" :class="{ overdue: it.overdue }"
               role="button" tabindex="0" :title="it.symptom || it.title"
               @click="jump.jumpToIncident(it.id)" @keydown.enter="jump.jumpToIncident(it.id)">
            <span class="dot" />
            <span class="cell-main">{{ it.title }}</span>
            <span class="tag">{{ it.category || '未分类' }}</span>
            <span class="days" :class="{ overdue: it.overdue }">
              {{ it.days_open === null ? '日期异常' : `挂起 ${it.days_open} 天` }}
            </span>
          </div>
        </template>
        <p v-else class="empty-line">没有未关闭的运维记录。</p>
      </section>

      <section class="card">
        <div class="card-head">
          <h3 class="section-title">最近笔记</h3>
          <span class="count-chip">{{ data?.recent_notes.length ?? 0 }}</span>
        </div>
        <template v-if="data?.recent_notes.length">
          <div v-for="n in data.recent_notes" :key="n.id" class="home-row"
               role="button" tabindex="0"
               @click="jump.jumpToNote(n.id)" @keydown.enter="jump.jumpToNote(n.id)">
            <span class="cell-main">{{ n.title }}</span>
            <span class="tag">{{ n.section }}</span>
            <span class="cell-time">{{ formatWhen(n.updated_at) }}</span>
          </div>
        </template>
        <p v-else class="empty-line">
          还没有公开笔记。<a class="link" @click="go('notes')">去记事本写一条</a>
        </p>
      </section>

      <section class="card">
        <div class="card-head"><h3 class="section-title">系统状态</h3></div>
        <div class="status-line">
          <span class="status-label">索引体积</span>
          <span v-if="data?.storage" class="status-value">
            {{ data.storage.db_mb }} MB
            <span class="muted">（空闲 {{ data.storage.free_mb }} MB）</span>
          </span>
          <span v-else class="status-value muted">不可用</span>
        </div>
        <div class="status-line">
          <span class="status-label">上次备份</span>
          <span class="status-value" :class="{ 'tone-warn': backupStale }">
            {{ data?.storage?.backup_at
              ? `${formatWhen(data.storage.backup_at)}（${data.storage.backup_days_ago} 天前）`
              : '从未备份' }}
          </span>
          <n-button size="tiny" quaternary @click="backupNow">立即备份</n-button>
        </div>
        <div class="status-line">
          <span class="status-label">文件监听</span>
          <span class="status-value">
            {{ data?.scan?.watched_roots
              ? `监听 ${data.scan.watched_roots} 个目录`
              : (counts?.roots ? '未监听（可在「索引目录」重新启用）' : '未启用（还没有索引目录）') }}
          </span>
        </div>
        <div v-if="data?.warnings.length" class="warn-line">
          部分区块降级：{{ data.warnings.join('；') }}
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.home { display: grid; gap: 14px; }
.head { align-items: baseline; }
.summary { margin: 0; font-size: 13px; color: var(--ink-2); }
.summary.error { color: var(--danger); }
.live { color: var(--info); }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
@media (max-width: 1080px) {
  .grid { grid-template-columns: 1fr; }
}
.card-head { display: flex; align-items: center; gap: 4px; margin-bottom: 6px; }
.stat-tile.clickable { cursor: pointer; }
.stat-tile.clickable:hover { border-color: var(--line-strong, var(--line)); background: var(--paper-2); }
.home-row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 4px; border-bottom: 1px solid var(--line-soft);
  cursor: pointer; border-radius: var(--r-xs);
}
.home-row:last-child { border-bottom: none; }
.home-row:hover { background: var(--paper-2); }
.home-row.overdue { box-shadow: inset 3px 0 0 var(--danger); padding-left: 8px; }
.cell-main { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; color: var(--ink); }
.cell-meta { flex: 0 0 auto; font-size: 11px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.cell-time { flex: 0 0 auto; font-size: 11px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.ext {
  flex: 0 0 auto; min-width: 34px; text-align: center;
  font-size: 11px; line-height: 18px; color: var(--ink-2);
  background: var(--paper-2); border-radius: var(--r-xs); padding: 0 4px;
}
.tag {
  flex: 0 0 auto; font-size: 11px; line-height: 18px; color: var(--ink-2);
  border: 1px solid var(--line); border-radius: var(--r-xs); padding: 0 5px;
  max-width: 96px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.days { flex: 0 0 auto; font-size: 11px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
.days.overdue { color: var(--danger); }
.dot { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%; background: var(--line); }
.home-row.overdue .dot { background: var(--danger); }
.empty-line { margin: 8px 0; font-size: 12px; color: var(--ink-3); }
.link { color: var(--info-deep); cursor: pointer; text-decoration: underline; }
.status-line { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--line-soft); }
.status-line:last-child { border-bottom: none; }
.status-label { flex: 0 0 68px; font-size: 12px; color: var(--ink-3); }
.status-value { flex: 1; min-width: 0; font-size: 13px; color: var(--ink); }
.tone-warn { color: var(--danger); }
.warn-line { margin-top: 8px; font-size: 11px; color: var(--danger); }
</style>
