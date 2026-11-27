<script setup lang="ts">
/**
 * 左侧一级导航：替代原顶部页签。
 * 垂直排布不再需要溢出下拉，按「知识 / 业务 / 系统」分组，底部显示运行形态与版本。
 * 版本号由 App.vue 从后端 /api/meta/status 取回后传入，本组件不持有版本常量。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '@/api/client'

interface NavItem { key: string; label: string; group?: string }

const props = defineProps<{ items: NavItem[]; active: string; version: string }>()
const emit = defineEmits<{ (e: 'update:active', key: string): void }>()

// 全局热键状态：不可用时在这里说明，而不是只写进日志（用户 2026-11-25 反馈过 1409）
interface HotkeyState { enabled: boolean; hotkey: string; error: string }
const hotkey = ref<HotkeyState | null>(null)

const prettyHotkey = computed(() =>
  (hotkey.value?.hotkey ?? '').split('+')
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join('+'))

onMounted(async () => {
  try {
    hotkey.value = await api<HotkeyState>('/api/hotkey/status')
  } catch {
    hotkey.value = null
  }
})

const GROUP_ORDER = ['知识', '业务', '系统']

const ICONS: Record<string, string[]> = {
  home: ['M4 10.2 12 3.6l8 6.6V20H4z', 'M9.8 20v-5.4h4.4V20'],
  search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Z', 'M16.2 16.2 21 21'],
  notes: ['M6 3h8l4 4v14H6z', 'M14 3v4h4', 'M9 12h6', 'M9 16h6'],
  projects: ['M4 4h6v6H4z', 'M14 4h6v6h-6z', 'M4 14h6v6H4z', 'M14 14h6v6h-6z'],
  incidents: ['M12 4 3.5 19.5h17L12 4Z', 'M12 10v4.5', 'M12 17v.5'],
  links: ['M10 14 14 10',
    'M8.6 12.4 6.2 14.8a3.4 3.4 0 0 0 4.8 4.8l2.4-2.4',
    'M15.4 11.6 17.8 9.2a3.4 3.4 0 0 0-4.8-4.8L10.6 6.8'],
  credentials: ['M6 11h12v9H6z', 'M9 11V8a3 3 0 0 1 6 0v3'],
  roots: ['M3 6h6l2 2h10v10.5H3z'],
}

/**
 * 兜底：新增页签若漏配图标，渲染一个小圆点占位，而不是留一个空的 16px svg。
 * 概览项曾因 ICONS 缺 home 整格空白，在 72px 图标栏下尤其像"空选择项"（2026-11-27 反馈）。
 */
const FALLBACK_ICON = ['M12 10.4a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2Z']

function iconPaths(key: string): string[] {
  return ICONS[key] ?? FALLBACK_ICON
}

const groups = computed(() => {
  const map = new Map<string, NavItem[]>()
  for (const it of props.items) {
    const g = it.group ?? '系统'
    if (!map.has(g)) map.set(g, [])
    map.get(g)!.push(it)
  }
  return GROUP_ORDER.filter(g => map.has(g)).map(g => ({ label: g, items: map.get(g)! }))
})
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-name">OnlyOne</div>
      <div class="brand-sub">个人工作台 · 本地索引</div>
    </div>

    <nav class="nav">
      <div v-for="g in groups" :key="g.label" class="nav-group">
        <div class="nav-group-label">{{ g.label }}</div>
        <button
          v-for="it in g.items" :key="it.key" type="button"
          class="nav-item" :class="{ active: props.active === it.key }"
          :aria-label="it.label" :title="it.label"
          :aria-current="props.active === it.key ? 'page' : undefined"
          @click="emit('update:active', it.key)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
               aria-hidden="true" focusable="false">
            <path v-for="(d, i) in iconPaths(it.key)" :key="i" :d="d" />
          </svg>
          <span class="nav-label">{{ it.label }}</span>
        </button>
      </div>
    </nav>

    <div class="footer">
      <div><span class="dot" />本地运行 · 无云端依赖</div>
      <div v-if="props.version">{{ props.version }}</div>
      <div v-if="hotkey" class="hotkey-line" :class="{ warn: !hotkey.enabled }"
           :title="hotkey.enabled ? 'Ctrl+Alt+Space 唤起快速捕获' : (hotkey.error || '热键未注册')">
        {{ hotkey.enabled ? '热键 ' + prettyHotkey : '热键不可用' }}
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 208px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border-right: 1px solid var(--line);
}
.brand { padding: 16px 16px 10px; }
.brand-name { font-size: 15px; font-weight: 500; color: var(--accent-strong); }
.brand-sub { margin-top: 3px; font-size: 11px; color: var(--ink-3); }
.nav { flex: 1; overflow-y: auto; padding: 2px 10px 10px; }
.nav-group { margin-top: 12px; }
.nav-group-label {
  padding: 0 8px 5px;
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--ink-3);
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--ink-2);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.nav-item:hover { background: var(--paper-2); color: var(--ink); }
/* 位置类语义：只靠 2px 橙条 + 文字加深表达"你在哪"，不再铺底色（规范 §8.6） */
.nav-item.active {
  background: transparent;
  color: var(--ink);
  font-weight: 500;
}
.nav-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  bottom: 7px;
  width: 2px;
  border-radius: 2px;
  background: var(--accent);
}
.nav-item svg { width: 16px; height: 16px; flex-shrink: 0; }
.nav-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.footer .hotkey-line { color: var(--ink-3); cursor: default; }
.footer .hotkey-line.warn { color: var(--warn); }
.footer {
  padding: 10px 14px 12px;
  border-top: 1px solid var(--line-soft);
  font-size: 11px;
  color: var(--ink-3);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 50%;
  background: var(--success);
}

/* 1024–1279：收成 72px 图标栏。文字一没，图标必须自己居中（否则整体左偏 9px） */
@media (max-width: 1279px) {
  .sidebar { width: 72px; }
  .brand { padding: 14px 0 8px; text-align: center; }
  .brand-name { font-size: 0; }
  .brand-name::first-letter { font-size: 15px; }
  .brand-sub { display: none; }
  .nav { padding: 2px 8px 8px; }
  .nav-group { margin-top: 8px; }
  .nav-group + .nav-group { border-top: 1px solid var(--line-soft); padding-top: 8px; }
  .nav-group-label { display: none; }
  .nav-item { justify-content: center; padding: 7px 0; }
  .nav-item svg { width: 20px; height: 20px; }
  .nav-label { display: none; }
  .footer { align-items: center; padding: 10px 0 12px; }
  .footer div { font-size: 0; }
}
</style>
