/**
 * `[[页面标题]]` 双链渲染（2026-11-25，功能 4）。
 *
 * 只做**装饰（Decoration）**，不改文档结构：`content_json` 里始终是原始方括号文本，
 * 因此导出 Markdown / HTML、参与全文检索、老页面打开都不受影响。
 * 点击由 NotesView 监听 `onlyone:wiki-open` 处理（查标题 → 跳页面）。
 */
import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

const WIKI_RE = /\[\[([^[\]\n]{1,80})\]\]/g

export const WikiLink = Extension.create({
  name: 'wikiLink',
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey('wikiLink'),
        props: {
          decorations(state) {
            const marks: Decoration[] = []
            state.doc.descendants((node, pos) => {
              if (!node.isText || !node.text) return
              WIKI_RE.lastIndex = 0
              let match: RegExpExecArray | null
              while ((match = WIKI_RE.exec(node.text))) {
                marks.push(Decoration.inline(pos + match.index, pos + match.index + match[0].length, {
                  class: 'wiki-link',
                  'data-wiki': match[1].trim(),
                }))
              }
            })
            return DecorationSet.create(state.doc, marks)
          },
          handleClick(_view, _pos, event) {
            const target = (event.target as HTMLElement | null)?.closest?.('.wiki-link') as
              HTMLElement | null
            if (!target) return false
            const title = target.getAttribute('data-wiki') || ''
            if (!title) return false
            window.dispatchEvent(new CustomEvent('onlyone:wiki-open', { detail: { title } }))
            return true
          },
        },
      }),
    ]
  },
})
