/** 运维记录的「类型」固定五档（M3-3 增补）。
 *
 * 运维记录页与项目工作台内嵌的运维表共用同一份定义，避免两处各写一遍导致选项漂移。
 * 库里直接存中文原值：导出 TSV / 直接查库都可读，调整选项也不需要迁移数据。
 * 历史记录该列为空串，界面上显示「—」，不做强行回填。
 */
import type { FieldOption } from './biz-types'

export const incidentCategoryOptions: FieldOption[] = [
  { label: 'BUG', value: 'BUG' },
  { label: '优化', value: '优化' },
  { label: '新需求', value: '新需求' },
  { label: '业务方案', value: '业务方案' },
  { label: '流程效率', value: '流程效率' },
]

/** 超期阈值（天）：未关闭且距「发生时间」超过该天数即视为超期。
 *  与后端 `/api/overview` 的默认 `days` 保持一致；比较用 `>`，恰好 7 天不算。 */
export const INCIDENT_OVERDUE_DAYS = 7

/** 挂起天数。口径与后端 `overview._days_since` 对齐：格式或日期非法返回 null，
 *  这类记录既不标黄也不计入超期，避免脏数据被算成「挂了 3000 天」。 */
export function incidentDaysOpen(raw: unknown): number | null {
  const text = String(raw ?? '').trim().slice(0, 16)
  if (text.length < 16) return null
  const moment = Date.parse(text.replace(' ', 'T'))
  if (Number.isNaN(moment)) return null
  // 浏览器会把 02-31 这类日期自动滚到 03-03，回读校验才能拒绝它们
  const date = new Date(moment)
  const [year, month, day] = text.slice(0, 10).split('-').map(Number)
  if (date.getFullYear() !== year || date.getMonth() + 1 !== month || date.getDate() !== day) {
    return null
  }
  return Math.floor((Date.now() - moment) / 86400000)
}

/** 传给 BizCrud 的行样式钩子：未关闭且超期 → `row-overdue`（styles.css 里有定义）。 */
export function incidentRowClass(row: Record<string, any>): string {
  if (String(row?.status ?? '') !== 'open') return ''
  const days = incidentDaysOpen(row?.occurred_at)
  return days !== null && days > INCIDENT_OVERDUE_DAYS ? 'row-overdue' : ''
}
