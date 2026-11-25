/**
 * 视图状态记忆（会话内，不落盘）：只记录"回到该页签时应该恢复到哪一条"。
 *
 * 背景：App.vue 用 v-if 切页签，切走即卸载视图，组件里的 ref 全部丢失——
 * 记事本因此每次回来都落到第一本笔记本的第一个分区。
 *
 * 存 pinia 而不是模块级变量：语义清晰、可被多个视图复用；也不写 localStorage，
 * 关闭程序即清空（用户 2026-11-24 选择"仅当前会话"）。
 */
import { defineStore } from 'pinia'

export interface NotesViewState {
  /** 当前笔记本 id */
  bookId: number | null
  /** 当前分区 id（页签） */
  sectionId: number | null
  /** 当前打开的页面 id；只选了分区没开页面时为 null */
  noteId: number | null
}

/** 列表筛选值：按筛选键（一般用表 kind）存一组 {字段: 值} */
export type FilterState = Record<string, any>

interface State {
  notes: NotesViewState
  filters: Record<string, FilterState>
}

export const useViewStateStore = defineStore('viewState', {
  state: (): State => ({
    notes: { bookId: null, sectionId: null, noteId: null },
    filters: {},
  }),
  actions: {
    setNotes(patch: Partial<NotesViewState>) {
      Object.assign(this.notes, patch)
    },
    setFilters(key: string, values: FilterState) {
      this.filters[key] = values
    },
    filtersFor(key: string): FilterState | null {
      return this.filters[key] ?? null
    },
  },
})
