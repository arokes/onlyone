/**
 * 图片压缩与上传的共享实现：记事本（NotesView）与业务记录富文本（RichTextField）共用。
 *
 * 抽出来的原因：两处都需要「>5MiB 先缩到最长边 2560、质量递减到阈值、再 base64 上传」，
 * 复制一份必然漂移（阈值、透明图处理、扩展名映射）。这里只有一份。
 */
import { api } from '@/api/client'

export const COMPRESS_THRESHOLD = 5 * 1024 * 1024   // 超过即前端压缩
export const BACKEND_LIMIT = 10 * 1024 * 1024       // 后端 MAX_IMAGE_BYTES，兜底
export const MAX_EDGE = 2560                        // 压缩后最长边
export const IMAGE_MIME_EXT: Record<string, string> = {
  'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp',
}

export interface UploadedImage {
  url: string
  filename: string
  size: number
  dedup: boolean
}

/** 超过 5 MiB 的位图等比缩放 + 质量递减；透明图保留 PNG，GIF 不动（会丢帧）。 */
export async function compressImage(file: File): Promise<File> {
  if (file.type === 'image/gif') return file
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    return file                        // 不支持解码时退回原图，由后端 10 MB 兜底
  }
  try {
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const w = Math.max(1, Math.round(bitmap.width * scale))
    const h = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = w
    canvas.height = h
    const ctx = canvas.getContext('2d')
    if (!ctx) return file
    ctx.drawImage(bitmap, 0, 0, w, h)
    let hasAlpha = false
    if (file.type === 'image/png' || file.type === 'image/webp') {
      const rgba = ctx.getImageData(0, 0, w, h).data
      for (let i = 3; i < rgba.length; i += 4) {
        if (rgba[i] < 255) { hasAlpha = true; break }
      }
    }
    const mime = hasAlpha ? 'image/png' : 'image/jpeg'
    let quality = 0.85
    let blob: Blob | null = null
    for (let attempt = 0, attempts = hasAlpha ? 1 : 6; attempt < attempts; attempt++) {
      blob = await new Promise<Blob | null>(resolve =>
        canvas.toBlob(resolve, mime, hasAlpha ? undefined : quality))
      if (blob && blob.size <= COMPRESS_THRESHOLD) break
      quality -= 0.12
      if (quality < 0.3) break
    }
    if (!blob || blob.size > BACKEND_LIMIT) return file
    const ext = IMAGE_MIME_EXT[mime]
    return new File([blob], (file.name.replace(/\.[^.]+$/, '') || 'image') + '.' + ext,
                    { type: mime })
  } finally {
    bitmap.close()
  }
}

export function normalizeMime(blob: Blob, src: string): string {
  if (IMAGE_MIME_EXT[blob.type]) return blob.type
  const m = /\.(png|jpe?g|gif|webp)(?:[?#]|$)/i.exec(src)
  if (!m) return ''
  const ext = m[1].toLowerCase()
  return ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg' : 'image/' + ext
}

export function dataUrlToFile(src: string): File | null {
  const m = /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$/i.exec(src)
  if (!m) return null
  const mime = m[1].toLowerCase()
  if (!IMAGE_MIME_EXT[mime]) return null   // svg+xml 等一律不接（后端魔数也会拒）
  try {
    const bin = atob(m[2].replace(/\s/g, ''))
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    return new File([bytes], 'pasted.' + IMAGE_MIME_EXT[mime], { type: mime })
  } catch {
    return null
  }
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}

/**
 * 压缩 + 上传，返回可直接写进富文本的 URL。
 *
 * 压缩失败或超限抛异常，由调用方决定提示文案；不在此处弹 message，
 * 避免工具模块耦合 UI 库。`path` 默认业务记录图片池，记事本传自己的页面端点。
 */
export async function uploadImageFile(
  file: File, path = '/api/attachments/images',
): Promise<UploadedImage> {
  if (!IMAGE_MIME_EXT[file.type]) {
    throw new Error('不支持的图片格式（仅 png / jpg / gif / webp）')
  }
  let target = file
  if (file.size > COMPRESS_THRESHOLD) target = await compressImage(file)
  if (target.size > BACKEND_LIMIT) throw new Error('图片超过 10 MB 上限')
  const dataUrl = await fileToDataUrl(target)
  return api<UploadedImage>(path, {
    method: 'POST', body: JSON.stringify({ data: dataUrl, name: target.name }),
  })
}
