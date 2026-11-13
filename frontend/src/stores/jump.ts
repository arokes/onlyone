/** 跨页跳转（对齐 ACMNG stores/jump）：从任意页签跳回全局搜索。 */
import { defineStore } from 'pinia'

interface JumpState {
  activeTab: string
  /** 跳到搜索页后按该词自动检索 */
  pendingQuery: string | null
  /** 跳到搜索页后直接打开该条目的只读预览 */
  pendingEntryId: number | null
}

// 支持 ?tab=credentials&q=灾备 这类深链接（也用于 headless 截图定位页签与自动检索）
const params = new URLSearchParams(window.location.search)
const initialTab = params.get('tab') ?? 'search'
const initialQuery = params.get('q')

export const useJumpStore = defineStore('jump', {
  state: (): JumpState => ({
    activeTab: initialTab,
    pendingQuery: initialQuery,
    pendingEntryId: null,
  }),
  actions: {
    jumpToSearch(query: string) {
      this.pendingQuery = query
      this.activeTab = 'search'
    },
    consumeQuery(): string | null {
      const q = this.pendingQuery
      this.pendingQuery = null
      return q
    },
    jumpToEntry(entryId: number) {
      this.pendingEntryId = entryId
      this.activeTab = 'search'
    },
    consumeEntryId(): number | null {
      const id = this.pendingEntryId
      this.pendingEntryId = null
      return id
    },
  },
})
