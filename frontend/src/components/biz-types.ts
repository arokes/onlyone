/** 业务表通用 CRUD 的字段描述（供 BizCrud 与四个业务页共用）。 */
export type FieldType =
  | 'text' | 'textarea' | 'number' | 'date' | 'datetime'
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
