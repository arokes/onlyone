<script setup lang="ts">
/**
 * 企微链接（M3-3 / M3-5）：只登记标题 + URL + 归属项目，
 * 不读取 wedoc 内容；点击标题用系统默认浏览器打开（后端做协议白名单校验）。
 */
import BizCrud from '@/components/BizCrud.vue'
import type { Field } from '@/components/biz-types'

const fields: Field[] = [
  { key: 'title', label: '标题', width: 200 },
  { key: 'url', label: '链接', type: 'url' },
  { key: 'project_id', label: '归属项目', type: 'project', width: 150 },
  { key: 'note', label: '备注', type: 'textarea', hideInTable: true },
]
</script>

<template>
  <div>
    <div class="card" style="margin-bottom: 14px">
      <div class="muted">
        官方限制：应用只能读写自己创建的文档，成员创建的文档无 API 可读 → 这里只登记链接，程序不读取文档内容。
      </div>
    </div>
    <biz-crud kind="links" title="企微链接" :fields="fields" />
  </div>
</template>
