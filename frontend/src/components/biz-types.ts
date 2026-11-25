/** 业务表通用 CRUD 的字段描述（供 BizCrud 与四个业务页共用）。 */
export type FieldType =
  | 'text' | 'textarea' | 'rich' | 'number' | 'date' | 'datetime'
  | 'select' | 'project' | 'url'

export interface FieldOption {
  label: string
  value: string | number
}

export interface Field {
  key: string
  label: string
  type?: FieldType
  options?: FieldOption[]
  width?: number
  /** 只在编辑表单出现，不占表格列（长文本字段用）。 */
  hideInTable?: boolean
}

/** 列表筛选控件类型 */
export type FilterType = 'keyword' | 'select' | 'project' | 'daterange' | 'text'

/**
 * 业务表筛选项声明（由页面传入，BizCrud 负责渲染与过滤）。
 * - keyword：跨全部字段的全文匹配（富文本先抽纯文本），空格分词 AND
 * - select：下拉精确匹配；不给 options 时从现有行去重生成
 * - project：归属项目精确匹配（选项来自项目列表）
 * - daterange：按日期闭区间比较该字段前 10 位（yyyy-MM-dd）
 * - text：子串模糊匹配
 */
export interface FilterSpec {
  key: string
  label: string
  type: FilterType
  options?: FieldOption[]
  placeholder?: string
}
