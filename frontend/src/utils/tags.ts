/** 用户标签的前端解析（2026-11-25）。
 *
 * 后端把 `entries.user_tags` 存成 JSON 数组字符串，列表与搜索结果都会原样带出。
 * 这里统一解析，避免每个视图各写一份 `JSON.parse` 容错。
 */
export function parseTags(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw.map(v => String(v).trim()).filter(Boolean)
  }
  if (typeof raw !== 'string' || !raw.trim()) return []
  try {
    const data = JSON.parse(raw)
    return Array.isArray(data) ? data.map(v => String(v).trim()).filter(Boolean) : []
  } catch {
    return []
  }
}
