<script setup lang="ts">
/**
 * 凭据库（M4-2，2026-11-09 调整）：全字段默认明文、点击行单选高亮、
 * 编辑/删除等动作收拢到工具栏、复制后 30 秒清剪贴板。
 * 存储层仍是 DPAPI 加密；明文只在本页内存与 API 响应中，不写日志、不进全文索引。
 */
import { computed, h, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  NButton, NDataTable, NInput, NModal, NSelect, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { api } from '@/api/client'

interface Credential {
  id: number
  title: string
  project_id: number | null
  updated_at: string
  username: string
  password: string
  url: string
  note: string
  has_username: boolean
  has_password: boolean
  has_url: boolean
  has_note: boolean
  sap_conn: string
}
interface Project { id: number; name: string }
interface SapSystem { key: string; label: string; sid: string; client: string; name: string }
interface SapConfig {
  sap_gui_dir: string
  configured_gui_dir: string
  landscape_xml: string
  configured_landscape_xml: string
  detected: boolean
  files: { saplogon: boolean; sapshcut: boolean; landscape: boolean }
  systems: number
}
interface SapLaunchResult {
  opened: string
  verified: boolean
  detail?: string
  front?: boolean
  launch_name?: string
  reused?: boolean
}

const ENC_FIELDS = [
  { key: 'username', label: '账号' },
  { key: 'password', label: '密码' },
  { key: 'url', label: '链接' },
  { key: 'note', label: '备注' },
]

const message = useMessage()
const dialog = useDialog()
const rows = ref<Credential[]>([])
const projects = ref<Project[]>([])
const sapSystems = ref<SapSystem[]>([])
const sapCfg = ref<SapConfig | null>(null)
const sapGuiInput = ref('')
const sapLandInput = ref('')
const sapSaving = ref(false)
const loading = ref(false)
const showModal = ref(false)
const saving = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<Record<string, any>>({})
const cleared = ref<string[]>([])
const selectedId = ref<number | null>(null)
let clearTimer: number | undefined

const selectedRow = computed(() =>
  rows.value.find(r => r.id === selectedId.value) ?? null,
)

/** 点击行单选高亮；工具栏按钮只对当前选中行生效 */
function rowProps(row: Credential) {
  return {
    class: row.id === selectedId.value ? 'row-selected' : '',
    style: 'cursor: pointer',
    onClick: () => { selectedId.value = row.id },
  }
}

// M7：凭据加密导出 / 恢复（C 方案）
const showExport = ref(false)
const showImport = ref(false)
const passphrase = ref('')
const importPassphrase = ref('')
const importPayload = ref<Record<string, unknown> | null>(null)
const importFileName = ref('')
const importMode = ref<'merge' | 'replace'>('merge')

const projectOptions = computed(() =>
  projects.value.map(p => ({ label: p.name, value: p.id as number })),
)
const sapOptions = computed(() =>
  sapSystems.value.map(s => ({ label: s.label, value: s.key })),
)

function projectLabel(id: number | null) {
  if (id == null) return '—'
  return projects.value.find(p => p.id === id)?.name ?? '—'
}

function sapLabel(key: string) {
  if (!key) return '—'
  return sapSystems.value.find(s => s.key === key)?.label ?? key
}

async function load() {
  loading.value = true
  try {
    const [creds, projs] = await Promise.all([
      api<Credential[]>('/api/credentials'),
      api<Project[]>('/api/biz/projects'),
    ])
    rows.value = creds
    projects.value = projs
  } catch (e) {
    message.error(String(e))
  } finally {
    loading.value = false
  }
  // 本机没装 SAP GUI 时只是下拉为空，不影响凭据库主流程
  try {
    sapSystems.value = await api<SapSystem[]>('/api/sap/systems')
  } catch {
    sapSystems.value = []
  }
  void loadSapConfig()
}
onMounted(load)

function resetForm() {
  Object.keys(form).forEach(k => delete form[k])
  cleared.value = []
}

function openCreate() {
  editingId.value = null
  resetForm()
  form.sap_conn = ''
  showModal.value = true
}

function openEdit(row: Credential) {
  editingId.value = row.id
  resetForm()
  form.title = row.title
  form.project_id = row.project_id
  form.sap_conn = row.sap_conn || ''
  // 列表已返回全部明文，直接回填；清空字段仍用弹窗里的「清空」标记。
  for (const f of ENC_FIELDS) form[f.key] = row[f.key as keyof Credential] ?? ''
  showModal.value = true
}

function toggleClear(key: string) {
  cleared.value = cleared.value.includes(key)
    ? cleared.value.filter(k => k !== key)
    : [...cleared.value, key]
}

async function save() {
  const data: Record<string, any> = {}
  const title = String(form.title || '').trim()
  if (!title) {
    message.warning('标题不能为空')
    return
  }
  data.title = title
  data.project_id = form.project_id ?? null
  data.sap_conn = form.sap_conn || ''
  for (const f of ENC_FIELDS) {
    if (cleared.value.includes(f.key)) data[f.key] = ''
    else if (editingId.value === null) data[f.key] = form[f.key] || ''
    else if (form[f.key]) data[f.key] = form[f.key]
  }
  saving.value = true
  try {
    const path = '/api/credentials' + (editingId.value ? '/' + editingId.value : '')
    await api(path, {
      method: editingId.value ? 'PUT' : 'POST',
      body: JSON.stringify(data),
    })
    showModal.value = false
    selectedId.value = null
    message.success('已保存')
    await load()
  } catch (e) {
    message.error(String(e))
  } finally {
    saving.value = false
  }
}

function remove(row: Credential) {
  dialog.warning({
    title: '确认删除',
    content: `删除「${row.title}」后，本机保存的密文会一并移除且不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api(`/api/credentials/${row.id}`, { method: 'DELETE' })
        message.success('已删除')
        selectedId.value = null
        await load()
      } catch (e) {
        message.error(String(e))
      }
    },
  })
}

/** A 档：服务端解密并打开链接，URL 不回显到前端 */
async function openUrl(row: Credential) {
  try {
    await api(`/api/credentials/${row.id}/open-url`, { method: 'POST' })
    message.success('已用默认浏览器打开链接')
  } catch (e) {
    message.error(String(e))
  }
}

/** 按凭据绑定的系统启动：后端自动填用户名、不填密码；前端自动复制密码 */
async function openSap() {
  const row = selectedRow.value
  if (!row || !row.sap_conn) {
    try {
      const r = await api<SapLaunchResult>('/api/open-sap', { method: 'POST' })
      message.success(r.reused ? 'SAP Logon 已在运行，已把窗口置前' : '已启动 SAP Logon')
    } catch (e) {
      message.error(String(e))
    }
    return
  }
  try {
    const r = await api<SapLaunchResult>(`/api/credentials/${row.id}/open-sap`, { method: 'POST' })
    if (r.verified) message.success(`已打开 ${r.launch_name || '绑定的 SAP 系统'}（自动填用户名）`)
    else message.warning(r.detail || '未能确认 SAP 会话窗口已打开，请检查任务栏')
    await copyField(row, 'password', true)
    message.info('密码已复制到剪贴板，30 秒后自动清空')
  } catch (e) {
    message.error(String(e))
  }
}

/** SAP 路径配置：支持原生文件夹选择、手动输入与自动探测 */
async function loadSapConfig() {
  try {
    const c = await api<SapConfig>('/api/sap/config')
    sapCfg.value = c
    sapGuiInput.value = c.configured_gui_dir
    sapLandInput.value = c.configured_landscape_xml
  } catch (e) {
    message.error(String(e))
  }
}

async function saveSapConfig() {
  sapSaving.value = true
  try {
    const c = await api<SapConfig>('/api/sap/config', {
      method: 'PUT',
      body: JSON.stringify({
        sap_gui_dir: sapGuiInput.value,
        landscape_xml: sapLandInput.value,
      }),
    })
    sapCfg.value = c
    sapGuiInput.value = c.configured_gui_dir
    sapLandInput.value = c.configured_landscape_xml
    message.success('SAP 路径已保存')
    await load()
  } catch (e) {
    message.error(String(e))
  } finally {
    sapSaving.value = false
  }
}

async function pickSapDir() {
  try {
    const r = await api<{ cancelled: boolean; path?: string; config?: SapConfig }>(
      '/api/sap/pick-dir', { method: 'POST' },
    )
    if (r.cancelled) return
    if (r.config) {
      sapCfg.value = r.config
      sapGuiInput.value = r.config.configured_gui_dir
      sapLandInput.value = r.config.configured_landscape_xml
    } else if (r.path) {
      sapGuiInput.value = r.path
    }
    message.success('已选择并保存 SAP GUI 目录')
    await load()
  } catch (e) {
    message.error(String(e))
  }
}

async function resetSapConfig() {
  sapGuiInput.value = ''
  sapLandInput.value = ''
  await saveSapConfig()
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

function scheduleClear(value: string) {
  if (clearTimer) window.clearTimeout(clearTimer)
  clearTimer = window.setTimeout(async () => {
    try {
      if (navigator.clipboard?.readText) {
        const current = await navigator.clipboard.readText()
        if (current && current !== value) return  // 用户已复制别的内容，不误清
      }
      await writeClipboard('')
      message.info('剪贴板已清空')
    } catch {
      try { await writeClipboard('') } catch { /* 无剪贴板权限时放弃 */ }
    }
  }, 30000)
}

async function copyField(row: Credential, field: string, silent = false) {
  // 列表已返回明文，无需再走 reveal 接口
  const value = String(row[field as keyof Credential] ?? '')
  if (!value) {
    message.warning('该字段为空')
    return
  }
  try {
    await writeClipboard(value)
    if (!silent) message.success('已复制，30 秒后自动清空剪贴板')
    scheduleClear(value)
  } catch (e) {
    message.error('复制失败：' + String(e))
  }
}

const columns = computed<DataTableColumns>(() => {
  const cols: DataTableColumns = [
    { title: '标题', key: 'title', width: 160, ellipsis: { tooltip: true } },
  ]
  for (const f of ENC_FIELDS) {
    cols.push({
      title: f.label,
      key: f.key,
      width: f.key === 'username' ? 230 : 210,
      // 复制动作统一收拢到工具栏，表格行内只展示内容
      render: (row: any) => h('div', { class: 'cell' }, [
        f.key === 'url' && row.url
          ? h('a', {
              class: 'cell-link', title: '点击用默认浏览器打开',
              onClick: () => void openUrl(row),
            }, row.url)
          : h('span', { class: 'cell-value ellipsis' }, row[f.key] || '—'),
      ]),
    })
  }
  cols.push({
    title: '归属项目', key: 'project_id', width: 120,
    render: (row: any) => projectLabel(row.project_id),
  })
  cols.push({
    title: 'SAP 系统', key: 'sap_conn', width: 160,
    render: (row: any) => sapLabel(row.sap_conn || ''),
  })
  return cols
})

async function exportCredentials() {
  if (passphrase.value.length < 8) {
    message.warning('导出口令至少 8 个字符')
    return
  }
  try {
    const payload = await api<Record<string, unknown>>('/api/credentials/export', {
      method: 'POST', body: JSON.stringify({ passphrase: passphrase.value }),
    })
    const url = URL.createObjectURL(new Blob(
      [JSON.stringify(payload, null, 2)], { type: 'application/json' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `onlyone-credentials-${Date.now()}.json`
    a.click()
    URL.revokeObjectURL(url)
    passphrase.value = ''
    showExport.value = false
    message.success('已导出加密文件（只含密文，可跨机恢复）')
  } catch (e) {
    message.error(String(e))
  }
}

async function onPickImport(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    importPayload.value = JSON.parse(await file.text())
    importFileName.value = file.name
  } catch {
    importPayload.value = null
    message.error('文件不是合法 JSON')
  }
  input.value = ''
}

async function importCredentials() {
  if (!importPayload.value) {
    message.warning('请先选择加密导出文件')
    return
  }
  if (importPassphrase.value.length < 8) {
    message.warning('请输入导出时设置的口令')
    return
  }
  try {
    const r = await api<{ imported: number }>('/api/credentials/import', {
      method: 'POST',
      body: JSON.stringify({
        passphrase: importPassphrase.value,
        payload: importPayload.value,
        mode: importMode.value,
      }),
    })
    message.success(`已恢复 ${r.imported} 条凭据（用当前 Windows 账户重新加密）`)
    showImport.value = false
    importPassphrase.value = ''
    importPayload.value = null
    importFileName.value = ''
    await load()
  } catch (e) {
    message.error('恢复失败（口令错误或文件损坏）：' + String(e))
  }
}

onUnmounted(() => {
  // 离开页签时立即清空，避免明文停留在剪贴板
  if (clearTimer) {
    window.clearTimeout(clearTimer)
    void writeClipboard('').catch(() => undefined)
  }
})
</script>

<template>
  <div class="card">
    <div class="muted">
      凭据使用 Windows DPAPI 加密，绑定当前 Windows 账户，无需主口令；数据库被拷到别的账户或机器无法解密，
      凭据不进全文索引。
      <br />
      按使用需求，<b>账号 / 密码 / 链接 / 备注全部默认明文显示</b>（屏幕与截图可见）；
      复制后 30 秒自动清空剪贴板，离开页签立即清空。
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <div class="row" style="margin-bottom: 10px">
      <h3 class="section-title">SAP 连接设置</h3>
      <div class="grow" />
      <span class="muted">识别到 {{ sapCfg?.systems ?? 0 }} 个连接项</span>
      <n-button size="small" @click="loadSapConfig">刷新状态</n-button>
    </div>
    <div class="sap-line">
      <span class="sap-key">SAP GUI 目录</span>
      <n-input v-model:value="sapGuiInput" size="small"
               placeholder="留空 = 自动探测（…\SAP\FrontEnd\SAPgui）" />
      <n-button size="small" @click="pickSapDir">选择文件夹…</n-button>
      <n-button size="small" type="primary" :loading="sapSaving" @click="saveSapConfig">保存</n-button>
      <n-button size="small" quaternary @click="resetSapConfig">自动探测</n-button>
    </div>
    <div class="sap-line" style="margin-top: 8px">
      <span class="sap-key">Landscape XML</span>
      <n-input v-model:value="sapLandInput" size="small"
               placeholder="留空 = %APPDATA%\SAP\Common\SAPUILandscape.xml（可选）" />
    </div>
    <div class="row" style="margin-top: 10px; flex-wrap: wrap; gap: 8px">
      <span class="muted">探测结果：</span>
      <span class="sap-chip" :class="sapCfg?.detected ? 'ok' : 'bad'">
        {{ sapCfg?.sap_gui_dir || '未找到安装目录' }}
      </span>
      <span class="sap-chip" :class="sapCfg?.files.sapshcut ? 'ok' : 'bad'">
        sapshcut.exe {{ sapCfg?.files.sapshcut ? '✓' : '✗' }}
      </span>
      <span class="sap-chip" :class="sapCfg?.files.landscape ? 'ok' : 'bad'">
        Landscape {{ sapCfg?.files.landscape ? '✓' : '✗' }}
      </span>
      <span class="muted">{{ sapCfg?.landscape_xml }}</span>
    </div>
    <div class="muted" style="margin-top: 8px">
      SAP GUI 8.00 起，启动参数必须用 SAP Logon 里的「连接项名称」（如 DS4-100ZH），
      旧版传 SID 会静默失败。目录或配置文件位置变化时在上面重新选择即可，无需改代码；
      启动后会校验会话窗口是否真的出现，并把 SAP 窗口置前。
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <h3 class="section-title" style="margin-bottom: 8px">导出 / 恢复（口令加密，可跨机迁移）</h3>
    <div class="muted">
      导出文件只含密文（Scrypt + AES-GCM），明文不落盘、不写日志；恢复时用当前 Windows 账户的 DPAPI
      重新加密，因此换机器也能用。日常使用仍然免主口令，口令只在导出 / 恢复时使用。
    </div>
    <div class="row" style="margin-top: 10px; gap: 8px">
      <n-button size="small" @click="showExport = true">加密导出凭据</n-button>
      <n-button size="small" @click="showImport = true">从加密文件恢复</n-button>
      <div class="grow" />
      <span class="muted">全库备份（记事本页）也会包含凭据密文</span>
    </div>
  </div>

  <div class="card" style="margin-top: 14px">
    <div class="row" style="margin-bottom: 10px; flex-wrap: wrap; gap: 6px">
      <h3 class="section-title">凭据库</h3>
      <span class="count-chip">{{ rows.length }}</span>
      <div class="grow" />
      <n-button size="small" @click="load">刷新</n-button>
      <n-button size="small" :disabled="!selectedRow" @click="openUrl(selectedRow!)">打开链接</n-button>
      <n-button size="small" @click="openSap">启动 SAP</n-button>
      <n-button size="small" :disabled="!selectedRow" @click="copyField(selectedRow!, 'username')">
        复制账号
      </n-button>
      <n-button size="small" :disabled="!selectedRow" @click="copyField(selectedRow!, 'password')">
        复制密码
      </n-button>
      <n-button size="small" :disabled="!selectedRow" @click="openEdit(selectedRow!)">编辑</n-button>
      <n-button size="small" quaternary type="error" :disabled="!selectedRow"
                @click="remove(selectedRow!)">
        删除
      </n-button>
      <n-button size="small" type="primary" @click="openCreate">新增</n-button>
    </div>
    <div class="muted" style="margin-bottom: 8px">
      点击表格行选中后，用上方按钮操作；「启动 SAP」按该凭据绑定的系统打开、自动填用户名，
      并自动复制密码（不填密码，30 秒后清空剪贴板）。
    </div>
    <n-data-table
      :columns="columns" :data="rows" :loading="loading" :row-props="rowProps"
      :bordered="false" size="small" :scroll-x="1220"
      :pagination="{ pageSize: 10 }"
    />
  </div>

  <n-modal
    v-model:show="showModal" preset="card"
    :title="(editingId ? '编辑' : '新增') + '凭据'" style="width: 560px"
  >
    <div class="form-row">
      <label class="form-label">标题</label>
      <n-input v-model:value="form.title" placeholder="例如：内网数据库" />
    </div>
    <div v-for="f in ENC_FIELDS" :key="f.key" class="form-row">
      <label class="form-label">{{ f.label }}</label>
      <n-input
        v-model:value="form[f.key]" type="text"
        :placeholder="editingId ? '清空请点右侧「清空」' : ''"
      />
      <n-button
        v-if="editingId" size="tiny" quaternary
        :type="cleared.includes(f.key) ? 'error' : 'default'"
        @click="toggleClear(f.key)"
      >
        {{ cleared.includes(f.key) ? '已标记清空' : '清空' }}
      </n-button>
    </div>
    <div class="form-row">
      <label class="form-label">归属项目</label>
      <n-select v-model:value="form.project_id" :options="projectOptions" clearable />
    </div>
    <div class="form-row">
      <label class="form-label">SAP 系统</label>
      <n-select v-model:value="form.sap_conn" :options="sapOptions" clearable
                placeholder="（不绑定，启动时只打开 SAP Logon）" />
    </div>
    <div class="muted" style="margin: -4px 0 10px 82px">
      绑定后点「启动 SAP」会打开该系统并自动填用户名；密码自动复制到剪贴板，需手动粘贴。
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showModal = false">取消</n-button>
        <n-button type="primary" :loading="saving" @click="save">保存</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showExport" preset="card" title="加密导出凭据" style="width: 430px">
    <div class="muted" style="margin-bottom: 10px">
      导出包含 {{ rows.length }} 条凭据，口令请用密码管理器另行保存；丢失口令无法恢复。
    </div>
    <n-input v-model:value="passphrase" type="password" show-password-on="click"
             placeholder="设置导出口令（至少 8 字符）" />
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showExport = false">取消</n-button>
        <n-button type="primary" @click="exportCredentials">导出加密文件</n-button>
      </div>
    </template>
  </n-modal>

  <n-modal v-model:show="showImport" preset="card" title="从加密文件恢复" style="width: 460px">
    <input type="file" accept=".json,application/json" @change="onPickImport" />
    <div class="muted" style="margin: 8px 0">{{ importFileName || '尚未选择文件' }}</div>
    <n-input v-model:value="importPassphrase" type="password" show-password-on="click"
             placeholder="导出时设置的口令" />
    <div class="row" style="margin-top: 10px">
      <span class="muted">导入方式</span>
      <n-select v-model:value="importMode" style="width: 160px"
                :options="[{ label: '追加到现有凭据', value: 'merge' }, { label: '清空后导入', value: 'replace' }]" />
    </div>
    <template #footer>
      <div class="row" style="justify-content: flex-end">
        <n-button @click="showImport = false">取消</n-button>
        <n-button type="primary" :disabled="!importPayload" @click="importCredentials">恢复</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.form-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.form-label { width: 72px; flex-shrink: 0; font-size: 13px; color: var(--ink-2); }
.form-row > :not(.form-label) { flex: 1; min-width: 0; }
.form-row > :deep(.n-button) { flex: 0 0 auto; }
:deep(.cell) { display: flex; align-items: center; gap: 4px; }
:deep(.cell-value) { flex: 1; min-width: 0; font-size: 12px; }
:deep(.cell-link) {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 12px; color: var(--info-deep); cursor: pointer; text-decoration: underline;
}
:deep(.row-selected td) { background: var(--accent-soft) !important; }
.sap-line { display: flex; align-items: center; gap: 8px; }
.sap-key { width: 104px; flex-shrink: 0; font-size: 12px; color: var(--ink-2); }
.sap-line > :deep(.n-input) { flex: 1; min-width: 0; }
.sap-line > :deep(.n-button) { flex: 0 0 auto; }
.sap-chip {
  font-size: 11px; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--line); color: var(--ink-3);
  max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sap-chip.ok { color: var(--success); border-color: var(--success); }
.sap-chip.bad { color: var(--danger); border-color: var(--danger); }
</style>
