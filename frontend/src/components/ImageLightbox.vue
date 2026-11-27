<script setup lang="ts">
/**
 * 原图查看层（2026-11-26）：运维记录 / 项目的富文本与记事本双击图片时打开。
 *
 * 为什么不做缩略图链路：图片本来就只有一份原文件（/api/attachments/...），
 * 之前的问题是编辑器里 max-width:100% 把 4K 截图压到 600 多像素、又没有放大入口，
 * 不是画质被降级（`utils/image.ts` 只在 >5 MiB 时压到最长边 2560）。
 *
 * 层级：Teleport 到 body 并给 4000，高于 naive-ui 弹窗（2000 档），
 * 否则从编辑弹窗里打开会被压在下面。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineProps<{ src: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

/** true = 等比缩到窗口内；false = 1:1 原始像素（超出可滚动） */
const fit = ref(true)
let prevOverflow = ''

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  prevOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'   // 预览期间锁住背后页面的滚动
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  document.body.style.overflow = prevOverflow
})
</script>

<template>
  <Teleport to="body">
    <div class="lb-mask" :class="{ 'is-pixel': !fit }" @click.self="emit('close')">
      <div class="lb-stage" @click.self="emit('close')">
        <img
          :src="src" class="lb-img" :class="{ 'is-pixel': !fit }" alt="原图"
          title="点击切换 1:1 原始像素 / 适应窗口"
          @click="fit = !fit"
        />
      </div>
      <div class="lb-bar">
        <span class="lb-tip">{{ fit
          ? '单击图片查看 1:1 原始像素 · Esc 或点空白处关闭'
          : '当前 1:1 原始像素 · 可滚动查看 · 再点一次恢复适应窗口' }}</span>
        <a class="lb-link" :href="src" target="_blank" rel="noopener">新窗口打开</a>
        <button type="button" class="lb-close" @click="emit('close')">关闭 (Esc)</button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.lb-mask {
  position: fixed;
  inset: 0;
  z-index: 4000;
  background: rgba(24, 20, 16, 0.86);
  overflow: hidden;
}
/* 1:1 时整层可滚动；flex 居中 + overflow 会把超长内容顶部裁掉，
   所以用「块级滚动容器 + 内部 min-height:100% 居中」的写法。 */
.lb-mask.is-pixel { overflow: auto; }
.lb-stage {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 48px 24px 64px;
}
.lb-img {
  max-width: 96vw;
  max-height: 84vh;
  object-fit: contain;
  background: #fff;
  border-radius: var(--radius-sm);
  box-shadow: 0 10px 48px rgba(0, 0, 0, 0.45);
  cursor: zoom-in;
}
.lb-img.is-pixel {
  max-width: none;
  max-height: none;
  cursor: zoom-out;
}
/* 1:1 时图片常比视口大：flex 居中会让左侧/顶部溢出到负方向、滚动条够不着，
   所以把舞台撑到内容宽度（max-content）并改为顶对齐，整张图都能滚到。 */
.lb-mask.is-pixel .lb-stage {
  width: max-content;
  min-width: 100%;
  align-items: flex-start;
}
.lb-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 18px;
  background: rgba(24, 20, 16, 0.92);
  border-top: 1px solid rgba(255, 255, 255, 0.12);
  font-size: 12px;
  color: rgba(255, 255, 255, 0.82);
}
.lb-tip { flex: 1; }
.lb-link,
.lb-close {
  color: #fff;
  font-family: inherit;
  font-size: 12px;
  text-decoration: none;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: var(--radius-sm);
  padding: 4px 10px;
  cursor: pointer;
}
.lb-link:hover,
.lb-close:hover { background: rgba(255, 255, 255, 0.22); }
</style>
