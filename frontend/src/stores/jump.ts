/** 跨页跳转（对齐 ACMNG stores/jump）：从任意页签跳回全局搜索。 */
import { defineStore } from 'pinia'

interface JumpState {
  activeTab: string
  /** 跳到搜索页后按该词自动检索 */
  pendingQuery: string | null
  /** 跳到搜索页后直接打开该条目的只读预览 */
  pendingEntryId: number | null
  /** 跳到记事本后直接打开该页面（项目工作台 → 记事本） */
  pendingNoteId: number | null
  /** 跳到运维记录后直接打开该条编辑弹窗（全局搜索 → 运维记录） */
  pendingIncidentId: number | null
}

// 支持 ?tab=credentials&q=灾备&entry=123 这类深链接（也用于 headless 截图定位页签、检索与预览）
// 无深链接时落在「概览」：空搜索框作为首屏没有信息量（2026-11-25 决定）。
const params = new URLSearchParams(window.location.search)
const initialTab = params.get('tab') ?? 'home'
const initialQuery = params.get('q')
const initialEntry = Number(params.get('entry')) || null

export const useJumpStore = defineStore('jump', {
  state: (): JumpState => ({
    activeTab: initialTab,
    pendingQuery: initialQuery,
    pendingEntryId: initialEntry,
    pendingNoteId: null,
    pendingIncidentId: null,
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
    /** 跳到搜索页并打开该条目的预览。
     *  ``query`` 可选：带上它会让搜索页同时以该词检索（概览点文件用文件名），
     *  这样关掉预览后看到的是「围绕这个文件的结果」，而不是一个空搜索页。 */
    jumpToEntry(entryId: number, query?: string) {
      this.pendingEntryId = entryId
      const text = String(query || '').trim()
      if (text) this.pendingQuery = text
      this.activeTab = 'search'
    },
    consumeEntryId(): number | null {
      const id = this.pendingEntryId
      this.pendingEntryId = null
      return id
    },
    jumpToNote(noteId: number) {
      this.pendingNoteId = noteId
      this.activeTab = 'notes'
    },
    consumeNoteId(): number | null {
      const id = this.pendingNoteId
      this.pendingNoteId = null
      return id
    },
    jumpToIncident(incidentId: number) {
      this.pendingIncidentId = incidentId
      this.activeTab = 'incidents'
    },
    consumeIncidentId(): number | null {
      const id = this.pendingIncidentId
      this.pendingIncidentId = null
      return id
    },
  },
})
