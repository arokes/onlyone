/**
 * 全局搜索状态（方案 A）：页签用 v-if 切换会销毁 SearchView，
 * 把检索状态提到 Pinia store，切回来时关键词与结果原样保留。
 *
 * 跨应用重启只保留关键词（localStorage）；结果里含正文片段，不落盘。
 */
import { defineStore } from 'pinia'

export interface SearchItem {
  id: number
  type: string
  title: string
  summary: string
  snippet: string
  abs_path: string | null
  root_kind: string | null
  extract_status: string | null
  score?: number
  via?: string
  url?: string
  source?: string
  hit_count?: number
  hit_in_title?: boolean
}

const QUERY_KEY = 'onlyone_search_q'

function savedQuery(): string {
  try {
    return localStorage.getItem(QUERY_KEY) ?? ''
  } catch {
    return '' // 隐私模式 / 禁用存储时忽略
  }
}

interface SearchResult {
  items: SearchItem[]
  semantic_items?: SearchItem[]
  plugin_items?: SearchItem[]
  mode: string
}

interface SearchState {
  q: string
  items: SearchItem[]
  semanticItems: SearchItem[]
  pluginItems: SearchItem[]
  mode: string
  searched: boolean
}

export const useSearchStore = defineStore('search', {
  state: (): SearchState => ({
    q: savedQuery(),
    items: [],
    semanticItems: [],
    pluginItems: [],
    mode: '',
    searched: false,
  }),
  actions: {
    /** 只持久化关键词；搜索结果保留在内存中 */
    persistQuery() {
      try {
        const value = this.q.trim()
        if (value) localStorage.setItem(QUERY_KEY, value)
        else localStorage.removeItem(QUERY_KEY)
      } catch {
        // localStorage 不可用时降级为「仅本次会话保留」
      }
    },
    setResult(result: SearchResult) {
      this.items = result.items
      this.semanticItems = result.semantic_items ?? []
      this.pluginItems = result.plugin_items ?? []
      this.mode = result.mode
    },
    clear() {
      this.items = []
      this.semanticItems = []
      this.pluginItems = []
      this.searched = false
    },
  },
})
