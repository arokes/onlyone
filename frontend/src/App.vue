<script setup lang="ts">
/**
 * 应用外壳（左侧一级导航 + 右侧内容区）。
 * 页签按需挂载；activeTab 放在 Pinia store，供跨页跳转与 ?tab= 深链接复用。
 */
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref } from 'vue'
import {
  NAlert, NConfigProvider, NDialogProvider, NMessageProvider, NSpin,
  dateZhCN, zhCN,
} from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import { storeToRefs } from 'pinia'
import SideNav from '@/components/SideNav.vue'
import CaptureOverlay from '@/components/CaptureOverlay.vue'
import SetupWizard from '@/components/SetupWizard.vue'
import { api } from '@/api/client'
import { useJumpStore } from '@/stores/jump'

// 独立捕获小窗（?capture=1）：只渲染浮层，不挂应用壳与任何页面
const captureOnly = new URLSearchParams(window.location.search).get('capture') === '1'

const HomeView = defineAsyncComponent(() => import('@/views/HomeView.vue'))
const SearchView = defineAsyncComponent(() => import('@/views/SearchView.vue'))
const ProjectsView = defineAsyncComponent(() => import('@/views/ProjectsView.vue'))
const IncidentsView = defineAsyncComponent(() => import('@/views/IncidentsView.vue'))
const LinksView = defineAsyncComponent(() => import('@/views/LinksView.vue'))
const NotesView = defineAsyncComponent(() => import('@/views/NotesView.vue'))
const CredentialsView = defineAsyncComponent(() => import('@/views/CredentialsView.vue'))
const RootsView = defineAsyncComponent(() => import('@/views/RootsView.vue'))
const PlaceholderView = defineAsyncComponent(() => import('@/views/PlaceholderView.vue'))

const navItems = [
  { key: 'home', label: '概览', group: '知识' },
  { key: 'search', label: '全局搜索', group: '知识' },
  { key: 'notes', label: '记事本', group: '知识' },
  { key: 'projects', label: '项目事项', group: '业务' },
  { key: 'incidents', label: '运维记录', group: '业务' },
  { key: 'links', label: '企微链接', group: '业务' },
  { key: 'credentials', label: '凭据库', group: '系统' },
  { key: 'roots', label: '索引目录', group: '系统' },
]

/**
 * 配色取自参考站的关系模型，但按工具型密度做了明度校正：
 * 深墨蓝 = 主操作，陶土橙 = 强调，雾蓝 = 信息/链接，绿色 = 成功。
 */
const themeOverrides: GlobalThemeOverrides = {
  common: {
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif',
    fontSize: '13px',
    fontSizeSmall: '12px',
    primaryColor: '#1f3040',
    primaryColorHover: '#2c4052',
    primaryColorPressed: '#16242f',
    primaryColorSuppl: '#2c4052',
    infoColor: '#3e6288',
    infoColorHover: '#4a719b',
    infoColorPressed: '#2e4d6c',
    infoColorSuppl: '#3e6288',
    successColor: '#4d7459',
    successColorHover: '#5b8668',
    successColorPressed: '#3f6349',
    successColorSuppl: '#4d7459',
    warningColor: '#b0512c',
    warningColorHover: '#bf5a36',
    warningColorPressed: '#a44524',
    warningColorSuppl: '#b0512c',
    errorColor: '#b03a2e',
    errorColorHover: '#c04a3c',
    errorColorPressed: '#94302a',
    errorColorSuppl: '#b03a2e',
    textColorBase: '#26231e',
    textColor1: '#26231e',
    textColor2: '#5c564c',
    textColor3: '#6f675b',
    borderColor: '#d9cbb5',
    dividerColor: '#d9cbb5',
    bodyColor: '#f4eee2',
    cardColor: '#fffdf9',
    modalColor: '#fffdf9',
    popoverColor: '#fffdf9',
    tableColor: '#fffdf9',
    tableHeaderColor: '#ece2d3',
    inputColor: '#fffdf9',
    hoverColor: '#f1e9dc',
    borderRadius: '6px',
    borderRadiusSmall: '4px',
  },
  // 二级页签沿用"位置类"语义：橙色指示条，不用墨蓝（墨蓝只留给实心主按钮）
  Tabs: {
    barColor: '#bf5a36',
    tabTextColorActiveLine: '#a44524',
    tabTextColorHoverLine: '#a44524',
    tabFontWeightActive: '500',
  },
}

const jump = useJumpStore()
const { activeTab } = storeToRefs(jump)
const ready = ref(false)
const error = ref('')
const showSetup = ref(false)
// 版本号唯一来源是后端 __version__（/api/meta/status），前端不再写死；
// 拿不到时留空，侧栏不显示版本行，避免出现只有 "v" 的残缺文本。
const appVersion = ref('')
const current = computed<{ label: string; stage: string }>(() => ({
  label: navItems.find(i => i.key === activeTab.value)?.label ?? '',
  stage: '',
}))

function onGlobalKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    activeTab.value = 'search'
    nextTick(() => {
      // 异步组件首次挂载可能晚于本次事件，做一次延迟兜底
      window.dispatchEvent(new Event('onlyone:focus-search'))
      window.setTimeout(() => window.dispatchEvent(new Event('onlyone:focus-search')), 120)
    })
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onGlobalKey)
  try {
    const st = await api<{ ok: boolean; version: string; counts: { roots: number } }>(
      '/api/meta/status')
    appVersion.value = st.version ?? ''
    ready.value = true
    // 没有任何索引根目录且未跳过时才弹向导
    showSetup.value = st.counts.roots === 0
      && !localStorage.getItem('onlyone_setup_skipped')
  } catch (e) {
    error.value = String(e)
  }
})
onUnmounted(() => window.removeEventListener('keydown', onGlobalKey))
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <div v-if="!captureOnly" class="app">
          <side-nav
            :items="navItems" :active="activeTab" :version="appVersion ? 'v' + appVersion : ''"
            @update:active="activeTab = $event"
          />
          <main class="content">
            <n-alert v-if="error" type="error" title="后端未就绪">
              {{ error }} —— 请确认 backend/run.py 已启动，或改用 npm run dev 的开发模式。
            </n-alert>
            <div v-else-if="!ready" class="center"><n-spin size="small" /> 正在连接本地服务…</div>
            <template v-else>
              <HomeView v-if="activeTab === 'home'" />
              <SearchView v-else-if="activeTab === 'search'" />
              <NotesView v-else-if="activeTab === 'notes'" />
              <ProjectsView v-else-if="activeTab === 'projects'" />
              <IncidentsView v-else-if="activeTab === 'incidents'" />
              <LinksView v-else-if="activeTab === 'links'" />
              <CredentialsView v-else-if="activeTab === 'credentials'" />
              <RootsView v-else-if="activeTab === 'roots'" />
              <PlaceholderView v-else :title="current.label" :stage="current.stage" />
            </template>
          </main>
          <setup-wizard v-model:show="showSetup" />
          <!-- 主窗口不再挂载快速捕获浮层：热键只走独立小窗（?capture=1）。
               之前两处都挂，主窗口那份也在轮询 /api/capture/pending，
               于是热键一按主窗口里也弹一张卡片，要按 ESC 才消失（2026-11-26 用户反馈）。 -->
        </div>
        <!-- 独立小窗模式：整页只有一张捕获卡片 -->
        <capture-overlay v-else standalone />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style scoped>
.app { height: 100vh; display: flex; background: var(--paper); }
/* 内容区铺满窗口：宽屏不设整体 max-width（否则最大化后两侧留白）。
   阅读版心交给内容自己控制——记事本正文 720px 居中，表格与列表按容器自适应。 */
.content {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  padding: 20px 24px 28px;
}
.center { display: flex; align-items: center; gap: 10px; padding: 40px; color: var(--ink-3); }
</style>
