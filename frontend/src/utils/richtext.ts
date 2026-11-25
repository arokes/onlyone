/**
 * 富文本列的前端读取：与后端 `onlyone.rich` 同一套 ProseMirror 结构。
 *
 * - `toEditorContent()` 把列值交给 TipTap（JSON 直接用；旧的纯文本按行包段落）。
 * - `docToText()` 用于列表单元格等「只要一行字」的地方，行为与后端
 *   `rich.extract_text` 对齐：块级节点补换行、图片节点不出字符。
 */
export type PMNode = { type?: string; text?: string; content?: PMNode[]; attrs?: Record<string, any> }
export interface PMDoc extends PMNode { type: 'doc' }

const BLOCKS = new Set([
  'paragraph', 'heading', 'codeBlock', 'blockquote', 'listItem',
  'bulletList', 'orderedList', 'table', 'tableRow',
])

/** 列值 → doc；不是合法 doc 时返回 null（老数据是纯文本）。 */
export function parseDoc(value: unknown): PMDoc | null {
  if (value == null || value === '') return null
  let raw: unknown = value
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text.startsWith('{')) return null
    try {
      raw = JSON.parse(text)
    } catch {
      return null
    }
  }
  if (typeof raw === 'object' && raw !== null && (raw as PMNode).type === 'doc') {
    return raw as PMDoc
  }
  return null
}

/** 纯文本 → 每行一个段落；空行跳过，全空时给一个空段落（TipTap 需要）。 */
export function textToDoc(text: string): PMDoc {
  const content = (text || '')
    .split(/\r?\n/)
    .filter(line => line.trim())
    .map(line => ({ type: 'paragraph', content: [{ type: 'text', text: line }] }))
  return { type: 'doc', content: content.length ? content : [{ type: 'paragraph' }] }
}

/** 交给编辑器的内容：JSON 原样，纯文本按行转段落，空值给空文档。 */
export function toEditorContent(value: unknown): PMDoc {
  return parseDoc(value) ?? textToDoc(typeof value === 'string' ? value : '')
}

/** doc → 纯文本（列表/摘要用）。与后端 rich.extract_text 保持同一规则。 */
export function docToText(value: unknown): string {
  if (value == null || value === '') return ''
  if (typeof value !== 'string') {
    // 已经是对象（理论上不会出现，防御性处理）
    return renderText(value as PMDoc)
  }
  const doc = parseDoc(value)
  if (!doc) return value.trim()
  return renderText(doc)
}

function renderText(node: PMNode | PMNode[] | undefined): string {
  const parts: string[] = []
  const walk = (n: any) => {
    if (Array.isArray(n)) { n.forEach(walk); return }
    if (!n || typeof n !== 'object') return
    if (n.type === 'text') parts.push(String(n.text ?? ''))
    else if (n.type === 'hardBreak') parts.push('\n')
    if (Array.isArray(n.content)) n.content.forEach(walk)
    if (BLOCKS.has(String(n.type))) parts.push('\n')
  }
  walk(node)
  return parts.join('').trim()
}

/** 图片节点数量（表格单元格显示「〔N 图〕」用）。 */
export function countImages(value: unknown): number {
  const doc = parseDoc(value)
  if (!doc) return 0
  let n = 0
  const walk = (node: any) => {
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (!node || typeof node !== 'object') return
    if (node.type === 'image') n++
    if (Array.isArray(node.content)) node.content.forEach(walk)
  }
  walk(doc.content ?? [])
  return n
}
