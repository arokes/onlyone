<script setup lang="ts">
/** 索引根目录管理（M5-2 / M6 / M8）：增删、后台扫描 + 进度、重建、监听、失败清单。 */
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import {
  NButton, NDataTable, NInput, NProgress, NSelect, NTag, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { api } from '@/api/client'

interface Root {
  id: number; path: string; kind: string; last_scan_at: string | null; enabled?: number
}
interface ScanStat {
  scanned: number; added: number; updated: number; removed: number; failed: number
  extracted: number; extract_failed: number; sensitive: number
  retried: number; given_up: number; seconds: number
}
interface ScanProgress {
  phase: 'idle' | 'queued' | 'scanning' | 'done' | 'error'
  root_id: number | null
  root_path: string
  done: number; total: number; percent: number
  current: string
  elapsed: number
  eta: number | null
  stats: ScanStat | Record<string, never>
  error: string
  queued: { root_id: number; root_path: string }[]
}
interface Failure {
  file_id: number; attempts: number; last_error: string; updated_at: string
  rel_path: string; abs_path: string; root_id: number; extract_status: string; root_path: string
}
interface WatcherStatus {
  enabled: boolean; watched_roots: number; scans: number
  last_event_at: string | null; last_error: string
}
interface OcrStatus {
  available: boolean; engine: string; mode: string
  templates_cached: boolean; tesseract: string | null
}
interface BackendStatus { available: boolean; enabled: { name: string }[] }
interface Counts { roots: number; files: number; entries: number }

const MAX_ATTEMPTS = 3
const message = useMessage()
const dialog = useDialog()
const roots = ref<Root[]>([])
const removing = ref<number | null>(null)
const failures = ref<Failure[]>([])
const watcher = ref<WatcherStatus | null>(null)
const ocr = ref<OcrStatus | null>(null)
const backends = ref<BackendStatus | null>(null)
const newPath = ref('')
const kind = ref('local')
const busy = ref(false)
const last = ref<ScanStat | null>(null)
const counts = ref<Counts | null>(null)
const progress = ref<ScanProgress | null>(null)
let timer: number | undefined
let pollTimer: number | undefined
let settled = true   // 本轮任务的结果是否已处理过，避免重复弹提示

const scanning = computed(() => {
  const p = progress.value
  return !!p && (p.phase === 'scanning' || p.phase === 'queued')
})
const queuedAhead = computed(() => progress.value?.queued.length ?? 0)

function fmtEta(seconds: number | null): string {
  if (seconds == null) return '估算中'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分 ${s % 60} 秒`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`
}

// ---------------------------------------------------------------- 重复文件
interface DupPath { rel_path: string; abs_path: string; mtime: number }
interface DupGroup {
  sha256: string; size: number; count: number; wasted: number; paths: DupPath[]
}
interface Duplicates {
  totals: {
    total_files: number; hashed_files: number; unhashed_files: number
    groups: number; redundant_files: number; wasted_bytes: number
  }
  groups: DupGroup[]
}
const dup = ref<Duplicates | null>(null)
const dupLoading = ref(false)

function fmtBytes(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(0)} KB`
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`
  return `${(bytes / 1073741824).toFixed(2)} GB`
}

/** 懒加载：重复检查要扫一遍 files 表，进页面不自动跑 */
async function loadDuplicates() {
  dupLoading.value = true
  try {
    dup.value = await api<Duplicates>('/api/duplicates?limit=50')
  } catch (e) {
    message.error(String(e))
  } finally {
    dupLoading.value = false
  }
}

async function revealPath(path: string) {
  try {
    await api('/api/open', { method: 'POST', body: JSON.stringify({ path, reveal: true }) })
  } catch (e) {
    message.error(String(e))
  }
}

async function copyPath(path: string) {
  try {
    await navigator.clipboard.writeText(path)
    message.success('已复制路径')
  } catch {
    message.error('复制失败')
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = window.setInterval(pollOnce, 800)
}

function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

/** 轮询扫描进度；任务结束时刷一次目录 / 失败清单 / 计数并给出结果提示。 */
async function pollOnce() {
  let st: ScanProgress
  try {
    st = await api<ScanProgress>('/api/scan/status')
  } catch {
    return
  }
  progress.value = st
  if (st.phase === 'scanning' || st.phase === 'queued') {
    settled = false
    startPolling()
    return
  }
  stopPolling()
  if (settled) return
  settled = true
  if (st.phase === 'done') {
    last.value = st.stats as ScanStat
    await Promise.all([load(), loadFailures(), loadCounts()])
    message.success('扫描完成')
  } else if (st.phase === 'error') {
    message.error('扫描失败：' + (st.error || '未知错误'))
  }
}

/** 提交后台扫描；已在队列中（409）只提示、不重复提交。 */
async function submitScan(id: number, force = false) {
  busy.value = true
  try {
    const suffix = force ? 'rebuild' : 'scan'
    await api(`/api/roots/${id}/${suffix}?wait=false`, { method: 'POST' })
    settled = false
    await pollOnce()
    startPolling()
  } catch (e) {
    const text = String(e)
    if (text.includes('409')) {
      message.info('该目录已在扫描队列中，进度见上方进度条')
      settled = false
      await pollOnce()
      startPolling()
    } else {
      message.error(text)
    }
  } finally {
    busy.value = false
  }
}

const kindOptions = [
  { label: '本地目录', value: 'local' },
  { label: '爱数同步盘', value: 'anyshare_sync' },
]

async function load() {
  try {
    roots.value = await api<Root[]>('/api/roots')
  } catch (e) {
    message.error(String(e))
  }
}

async function loadWatcher() {
  try {
    watcher.value = await api<WatcherStatus>('/api/watcher/status')
  } catch { /* watcher 状态不可用时静默 */ }
}

async function loadCapabilities() {
  try {
    const [o, b] = await Promise.all([
      api<OcrStatus>('/api/ocr/status'),
      api<BackendStatus>('/api/backends/status'),
    ])
    ocr.value = o
    backends.value = b
  } catch { /* 扩展能力状态不可用时静默 */ }
}

async function loadFailures() {
  try {
    failures.value = await api<Failure[]>('/api/failures')
  } catch (e) {
    message.error(String(e))
  }
}

async function loadCounts() {
  try {
    const st = await api<{ counts: { roots: number; files: number; entries: number } }>(
      '/api/meta/status',
    )
    counts.value = st.counts
  } catch { /* 计数不可用时静默，不阻塞目录管理 */ }
}

async function add() {
  if (!newPath.value.trim()) return
  busy.value = true
  try {
    await api('/api/roots', {
      method: 'POST',
      body: JSON.stringify({ path: newPath.value.trim(), kind: kind.value }),
    })
    newPath.value = ''
    await load()
    await loadWatcher()
    message.success('已添加索引根目录')
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

function scan(id: number) {
  return submitScan(id, false)
}

function rebuild(id: number) {
  return submitScan(id, true)
}

async function retry(row: Failure) {
  busy.value = true
  try {
    await api(`/api/failures/${row.file_id}/retry?wait=false`, { method: 'POST' })
    await loadFailures()
    settled = false
    await pollOnce()
    startPolling()
    message.success('已重新提交抽取')
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

async function doRemove(id: number) {
  removing.value = id
  try {
    await api(`/api/roots/${id}`, { method: 'DELETE' })
    message.success('已移除索引目录')
    await Promise.all([load(), loadWatcher(), loadFailures(), loadCounts()])
  } catch (e) {
    message.error(String(e))
  } finally {
    removing.value = null
  }
}

function remove(id: number) {
  const target = roots.value.find(r => r.id === id)
  dialog.warning({
    title: '确认移除索引目录',
    content: `将移除「${target?.path ?? id}」及其全部索引数据，磁盘上的文件不会被删除。`,
    positiveText: '移除',
    negativeText: '取消',
    onPositiveClick: () => { void doRemove(id) },
  })
}

const failureColumns = computed<DataTableColumns>(() => [
  { title: '文件', key: 'rel_path', width: 220, ellipsis: { tooltip: true } },
  { title: '根目录', key: 'root_path', width: 200, ellipsis: { tooltip: true } },
  {
    title: '重试次数', key: 'attempts', width: 110,
    render: (row: any) => h(NTag, {
      size: 'small', bordered: false,
      type: row.attempts >= MAX_ATTEMPTS ? 'error' : 'warning',
    }, { default: () => `${row.attempts} / ${MAX_ATTEMPTS}` }),
  },
  { title: '错误', key: 'last_error', ellipsis: { tooltip: true } },
  {
    title: '操作', key: '__actions', width: 100, fixed: 'right',
    render: (row: any) => h(NButton, {
      size: 'tiny', secondary: true, onClick: () => retry(row),
    }, { default: () => '重新抽取' }),
  },
])

onMounted(async () => {
  await Promise.all([load(), loadWatcher(), loadFailures(), loadCapabilities(), loadCounts()])
  timer = window.setInterval(loadWatcher, 5000)
  // 启动校准 / watcher 增量可能已经在跑：进入页面即接管进度显示。
  await pollOnce()
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  stopPolling()
})
</script>

<template>
  <div v-if="counts" class="stat-strip" style="margin-bottom: 14px">
    <div class="stat-tile">
      <div class="stat-label">索引目录</div>
      <div class="stat-value">{{ counts.roots }}</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">索引文件</div>
      <div class="stat-value">{{ counts.files }}</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">全库条目</div>
      <div class="stat-value">{{ counts.entries }}</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">抽取失败</div>
      <div class="stat-value" :class="failures.length ? 'warn' : 'ok'">{{ failures.length }}</div>
    </div>
  </div>

  <div class="card">
    <h3 class="section-title" style="margin-bottom: 12px">添加索引根目录</h3>
    <div class="row">
      <n-select v-model:value="kind" :options="kindOptions" style="width: 150px" />
      <n-input v-model:value="newPath" class="grow" placeholder="例如 D:\Work\运维 或爱数同步盘的本地目录" />
      <n-button type="primary" :loading="busy" @click="add">添加</n-button>
    </div>
    <div class="muted" style="margin-top: 8px">
      权限由爱数客户端控制：只有你有权缓存/同步下来的目录才会出现在本地，程序不会越权访问云端。
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <h3 class="section-title" style="margin-bottom: 8px">扩展能力</h3>
    <div v-if="ocr && backends" class="muted">
      <div>
        OCR：<n-tag size="small" :bordered="false" :type="ocr.available ? 'success' : 'default'">
          {{ ocr.available ? ocr.engine : '不可用' }}
        </n-tag>
        <span v-if="ocr.available"> · {{ ocr.mode }} · 模板缓存{{ ocr.templates_cached ? '已生成' : '未生成' }}</span>
      </div>
      <div style="margin-top: 6px">
        语义检索：本地稀疏向量（同义词归一 + 字符 n-gram + 余弦），结果在搜索页独立分组。
      </div>
      <div style="margin-top: 6px">
        外部索引：<n-tag size="small" :bordered="false" :type="backends.available ? 'success' : 'default'">
          {{ backends.available ? '已启用 ' + backends.enabled.map(b => b.name).join(', ') : '默认关闭' }}
        </n-tag>
        <span v-if="!backends.available"> · 未检测到 CLI，行为与本地检索完全一致</span>
      </div>
      <div style="margin-top: 6px">
        图片扫描件与无文本层 PDF 会离线识别正文；含密码关键词的文件仍按硬约束排除。
      </div>
    </div>
    <div v-else class="muted">正在读取扩展能力状态…</div>
  </div>

  <div class="card" style="margin-top: 14px">
    <h3 class="section-title" style="margin-bottom: 8px">自动监听</h3>
    <div v-if="watcher" class="muted">
      <n-tag size="small" :bordered="false" :type="watcher.enabled ? 'success' : 'default'">
        {{ watcher.enabled ? '运行中' : '未启用' }}
      </n-tag>
      <span style="margin-left: 10px">
        监听目录 {{ watcher.watched_roots }} 个 · 自动扫描 {{ watcher.scans }} 次 ·
        最近事件 {{ watcher.last_event_at ?? '无' }}
      </span>
      <span v-if="watcher.last_error" style="color: var(--danger)"> · 上次错误：{{ watcher.last_error }}</span>
    </div>
    <div v-else class="muted">正在读取监听状态…</div>
    <div class="muted" style="margin-top: 6px">
      生产形态（桌面窗口）自动启用；文件变更后约 1.2 秒防抖触发增量扫描，启动时做一次全量校准。
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <div class="row" style="margin-bottom: 10px">
      <h3 class="section-title">已登记目录</h3>
      <span class="count-chip">{{ roots.length }}</span>
    </div>

    <div v-if="scanning" class="scan-box">
      <div class="row" style="gap: 10px; margin-bottom: 6px">
        <span class="scan-phase">
          {{ progress?.phase === 'queued' ? '排队中' : '正在扫描' }}
        </span>
        <span class="muted ellipsis" style="max-width: 340px">{{ progress?.root_path }}</span>
        <div class="grow" />
        <span class="muted">
          {{ progress?.done ?? 0 }} / {{ progress?.total ?? 0 }} 个文件
          · 已用 {{ fmtElapsed(progress?.elapsed ?? 0) }}
          <template v-if="progress?.eta != null"> · 预计剩余 {{ fmtEta(progress?.eta ?? null) }}</template>
        </span>
      </div>
      <n-progress
        type="line" :percentage="progress?.percent ?? 0" :height="8" :border-radius="4"
        :show-indicator="false" color="var(--accent)" rail-color="var(--line-soft)"
      />
      <div class="row" style="gap: 10px; margin-top: 6px">
        <span class="muted ellipsis grow">
          {{ progress?.phase === 'queued'
            ? '等待前一个扫描任务结束…'
            : (progress?.current || '正在枚举目录…') }}
        </span>
        <span class="muted">{{ progress?.percent ?? 0 }}%</span>
      </div>
      <div v-if="queuedAhead" class="muted" style="margin-top: 4px">
        另有 {{ queuedAhead }} 个扫描任务排队
      </div>
    </div>

    <div v-if="!roots.length" class="muted">还没有目录。</div>
    <div v-for="r in roots" :key="r.id" class="list-row">
      <n-tag size="small" :bordered="false" :type="r.kind === 'anyshare_sync' ? 'info' : 'default'">
        {{ r.kind === 'anyshare_sync' ? '爱数同步盘' : '本地' }}
      </n-tag>
      <n-tag v-if="r.enabled === 0" size="small" :bordered="false" type="warning">已停用</n-tag>
      <n-tag
        v-if="scanning && progress?.root_id === r.id" size="small" :bordered="false" type="success"
      >
        {{ progress?.phase === 'queued' ? '排队中' : '扫描中' }}
      </n-tag>
      <div class="grow">
        <div class="ellipsis">{{ r.path }}</div>
        <div class="muted">上次扫描：{{ r.last_scan_at ?? '尚未扫描' }}</div>
      </div>
      <n-button size="tiny" secondary :loading="busy" @click="scan(r.id)">扫描</n-button>
      <n-button size="tiny" quaternary :loading="busy" @click="rebuild(r.id)">重建索引</n-button>
      <n-button
        size="tiny" quaternary :loading="removing === r.id" :disabled="removing !== null"
        @click="remove(r.id)"
      >
        移除
      </n-button>
    </div>
    <div v-if="last" class="muted" style="margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line-soft)">
      最近一次扫描：{{ last.scanned }} 个文件，新增 {{ last.added }}，更新 {{ last.updated }}，
      移除 {{ last.removed }}，失败 {{ last.failed }}，正文抽取 {{ last.extracted }}，
      抽取失败 {{ last.extract_failed }}，重试 {{ last.retried }}，放弃 {{ last.given_up }}，
      敏感排除 {{ last.sensitive }}，耗时 {{ last.seconds }}s
    </div>
    <div v-if="last && last.sensitive" class="muted" style="margin-top: 6px">
      敏感排除为文件名命中「密码 / 口令 / 凭据 / password」等关键词的文件，按硬约束它们不进入全文索引。
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <div class="row" style="margin-bottom: 8px">
      <h3 class="section-title">抽取失败清单</h3>
      <span class="count-chip">{{ failures.length }}</span>
      <div class="grow" />
      <n-button size="tiny" quaternary @click="loadFailures">刷新</n-button>
    </div>
    <div v-if="!failures.length" class="muted">没有抽取失败的文件。</div>
    <n-data-table
      v-else :columns="failureColumns" :data="failures" :bordered="false"
      size="small" :scroll-x="900" :pagination="{ pageSize: 5 }"
    />
    <div class="muted" style="margin-top: 8px">
      失败会自动重试，累计 {{ MAX_ATTEMPTS }} 次后停止自动重试并留在清单中，可点「重新抽取」手动重置次数。
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <div class="row" style="margin-bottom: 8px">
      <h3 class="section-title">重复文件</h3>
      <span class="count-chip">{{ dup?.totals.groups ?? '—' }}</span>
      <div class="grow" />
      <n-button size="tiny" quaternary :loading="dupLoading" @click="loadDuplicates">
        {{ dup ? '重新检查' : '检查重复' }}
      </n-button>
    </div>

    <div v-if="!dup" class="muted">
      按文件内容哈希比对，只报告不删除。仅 ≤50 MB 的文件在扫描时计算了哈希，
      更大的文件不参与比对（数量会在下面如实显示）。
    </div>

    <template v-else>
      <div class="dup-strip">
        <div class="dup-tile">
          <div class="dup-label">重复组</div>
          <div class="dup-value">{{ dup.totals.groups }}</div>
        </div>
        <div class="dup-tile">
          <div class="dup-label">冗余文件</div>
          <div class="dup-value">{{ dup.totals.redundant_files }}</div>
        </div>
        <div class="dup-tile">
          <div class="dup-label">可释放</div>
          <div class="dup-value">{{ fmtBytes(dup.totals.wasted_bytes) }}</div>
        </div>
        <div class="dup-tile">
          <div class="dup-label">参与比对</div>
          <div class="dup-value">{{ dup.totals.hashed_files }}</div>
        </div>
      </div>

      <div v-if="!dup.groups.length" class="muted">没有发现重复文件。</div>

      <div v-for="g in dup.groups" :key="g.sha256" class="dup-group">
        <div class="dup-head">
          <code>{{ g.sha256.slice(0, 8) }}</code>
          <span>{{ g.count }} 份 · 每份 {{ fmtBytes(g.size) }}</span>
          <span class="dup-waste">可释放 {{ fmtBytes(g.wasted) }}</span>
        </div>
        <div v-for="p in g.paths" :key="p.abs_path" class="dup-path">
          <span class="dup-name" :title="p.abs_path">{{ p.rel_path }}</span>
          <span class="dup-act" @click="revealPath(p.abs_path)">定位</span>
          <span class="dup-act" @click="copyPath(p.abs_path)">复制路径</span>
        </div>
      </div>

      <div class="muted" style="margin-top: 8px">
        可释放 = 每组保留一份后能省下的空间。这里不提供一键删除，避免误删只存在于索引里的旧路径。
      </div>
    </template>
  </div>
</template>

<style scoped>
.scan-box {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm, 8px);
  background: #fdf7f1;
}
.scan-phase {
  font-size: 12px;
  font-weight: 600;
  color: var(--accent-strong);
  letter-spacing: 0.04em;
}
.dup-strip { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.dup-tile {
  flex: 1 1 110px; padding: 8px 10px;
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-sm);
}
.dup-label { font-size: 12px; color: var(--ink-3); }
.dup-value { font-size: 18px; font-weight: 500; color: var(--ink); font-variant-numeric: tabular-nums; }
.dup-group { padding: 8px 0; border-bottom: 1px solid var(--line-soft); }
.dup-group:last-child { border-bottom: none; }
.dup-head { display: flex; align-items: baseline; gap: 8px; font-size: 12px; color: var(--ink-2); }
.dup-head code { font-size: 11px; color: var(--ink-3); }
.dup-waste { margin-left: auto; color: var(--warn); }
.dup-path { display: flex; align-items: center; gap: 8px; padding: 3px 0 3px 10px; font-size: 12px; color: var(--ink-2); }
.dup-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dup-act { flex: 0 0 auto; color: var(--info-deep); cursor: pointer; text-decoration: underline; }
</style>
