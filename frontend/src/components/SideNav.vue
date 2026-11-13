<script setup lang="ts">
/**
 * 左侧一级导航（v0.8）：替代原顶部页签。
 * 垂直排布不再需要溢出下拉，按「知识 / 业务 / 系统」分组，底部显示运行形态与版本。
 */
import { computed } from 'vue'

interface NavItem { key: string; label: string; group?: string }

const props = defineProps<{ items: NavItem[]; active: string; version: string }>()
const emit = defineEmits<{ (e: 'update:active', key: string): void }>()

const GROUP_ORDER = ['知识', '业务', '系统']

const ICONS: Record<string, string[]> = {
  search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Z', 'M16.2 16.2 21 21'],
  notes: ['M6 3h8l4 4v14H6z', 'M14 3v4h4', 'M9 12h6', 'M9 16h6'],
  projects: ['M4 4h6v6H4z', 'M14 4h6v6h-6z', 'M4 14h6v6H4z', 'M14 14h6v6h-6z'],
  worklogs: ['M5 6h14', 'M5 12h14', 'M5 18h9'],
  incidents: ['M12 4 3.5 19.5h17L12 4Z', 'M12 10v4.5', 'M12 17v.5'],
  links: ['M10 14 14 10',
    'M8.6 12.4 6.2 14.8a3.4 3.4 0 0 0 4.8 4.8l2.4-2.4',
    'M15.4 11.6 17.8 9.2a3.4 3.4 0 0 0-4.8-4.8L10.6 6.8'],
  credentials: ['M6 11h12v9H6z', 'M9 11V8a3 3 0 0 1 6 0v3'],
  roots: ['M3 6h6l2 2h10v10.5H3z'],
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
          @click="emit('update:active', it.key)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path v-for="(d, i) in ICONS[it.key] ?? []" :key="i" :d="d" />
          </svg>
          <span class="nav-label">{{ it.label }}</span>
        </button>
      </div>
    </nav>

    <div class="footer">
      <div><span class="dot" />本地运行 · 无云端依赖</div>
      <div>{{ props.version }}</div>
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
.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-weight: 500;
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
</style>
