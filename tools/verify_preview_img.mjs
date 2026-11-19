#!/usr/bin/env node
/**
 * 只读预览「图片按原位内联」的回归（CDP，零第三方依赖）。
 *
 * 覆盖：后端 [图 N] 锚点与 images 序号对齐；前端块级 / 行内 / 单元格三种形态
 * 都出真图（naturalWidth > 0、无裂图）；底部网格默认折叠、点击后展开。
 * 产出 docs/screenshots/09-preview-inline-img.png。
 *
 * 用法：node tools/verify_preview_img.mjs [--entry 2263]
 * 前置：npm run build 已生成 frontend/dist；本机 Chrome 路径见 CHROME。
 * 注意：本脚本读项目库（data/onlyone.db），只做检索与预览，不写入。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const SHOT = join(ROOT, 'docs', 'screenshots',
  (process.argv.find(a => a.startsWith('--shot=')) ?? '').slice(7) || '09-preview-inline-img.png')
const TOKEN = 'preview-img-verify-20261117'
const ENTRY = Number((process.argv.find(a => a.startsWith('--entry=')) ?? '').split('=')[1]) || 2263
const QUERY = (process.argv.find(a => a.startsWith('--q=')) ?? '').slice(4) || 'SAP通用操作指南'

const results = []
const sleep = ms => new Promise(r => setTimeout(r, ms))
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS ' : 'FAIL '}${label}${detail ? '  ' + detail : ''}`)
  results.push(ok)
}
function freePort() {
  return new Promise(res => {
    const s = createServer()
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)) })
  })
}
async function waitFor(label, probe, timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { if (await probe()) return true } catch { /* 尚未就绪 */ }
    await sleep(250)
  }
  throw new Error(`${label} 超时`)
}

let backend, chrome, ws, profileDir
let seq = 0
const pending = new Map()
function cdp(method, params = {}) {
  const id = ++seq
  return new Promise((res, rej) => {
    pending.set(id, { res, rej })
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression, awaitPromise = false) {
  const r = await cdp('Runtime.evaluate', { expression, awaitPromise, returnByValue: true })
  if (r.exceptionDetails)
    throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text)
  return r.result.value
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_pvimg_'))
  const port = await freePort()
  const chromePort = await freePort()
  const base = `http://127.0.0.1:${port}`

  const env = { ...process.env, ONLYONE_TOKEN: TOKEN }
  delete env.ONLYONE_DATA_DIR
  backend = spawn('py', ['-3', join(ROOT, 'backend', 'run.py'),
                         '--no-window', '--no-watcher', '--port', String(port)], {
    cwd: join(ROOT, 'backend'), env, stdio: 'ignore',
  })
  await waitFor('后端就绪', async () => (await fetch(base + '/api/meta/status')).ok)

  async function api(path) {
    const r = await fetch(base + path, { headers: { 'X-OnlyOne-Token': TOKEN } })
    if (!r.ok) throw new Error(`${path} ${r.status} ${await r.text()}`)
    return r.json()
  }

  // 1) 后端：锚点与图片序号必须一一对齐
  const pv = await api('/api/preview?entry_id=' + ENTRY)
  const marks = [...(pv.text ?? '').matchAll(/\[图 (\d+)\]/g)].map(m => Number(m[1]))
  const imgs = pv.images ?? []
  check('预览返回图片元数据', imgs.length > 0, `${imgs.length} 张`)
  check('正文含 [图 N] 锚点', marks.length > 0, `${marks.length} 处`)
  const maxMark = Math.max(0, ...marks)
  check('锚点序号不越界', maxMark <= imgs.length, `最大 图${maxMark} / 共 ${imgs.length}`)
  check('图片序号连续', imgs.every((m, i) => m.index === i), imgs.slice(0, 3).map(m => m.index).join(','))

  // 2) 打开界面
  chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--remote-allow-origins=*',
    `--remote-debugging-port=${chromePort}`, `--user-data-dir=${profileDir}`,
    '--window-size=1440,1000', 'about:blank',
  ], { stdio: 'ignore' })
  const devtools = `http://127.0.0.1:${chromePort}`
  await waitFor('Chrome DevTools 就绪', async () => (await fetch(devtools + '/json/version')).ok)
  const targets = await (await fetch(devtools + '/json/list')).json()
  const page = targets.find(t => t.type === 'page')
  if (!page) throw new Error('没有可用的 Chrome page target')

  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    ws.onopen = res
    ws.onerror = () => rej(new Error('CDP WebSocket 连接失败'))
  })
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data)
    if (!msg.id || !pending.has(msg.id)) return
    const { res, rej } = pending.get(msg.id)
    pending.delete(msg.id)
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
  }
  await cdp('Page.enable')
  await cdp('Runtime.enable')
  await cdp('Emulation.setDeviceMetricsOverride',
            { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false })

  const url = `${base}/?token=${TOKEN}&tab=search&q=${encodeURIComponent(QUERY)}`
  await cdp('Page.navigate', { url })
  await waitFor('页面加载', async () => await evaluate(`document.readyState === 'complete'`))
  await evaluate(`localStorage.setItem('onlyone_setup_skipped', '1'); true`)
  await waitFor('搜索结果渲染', async () =>
    await evaluate(`document.querySelectorAll('.hit').length > 0`))

  const title = await evaluate(`document.querySelector('.hit .hit-title')?.textContent.trim() ?? ''`)
  check('搜索命中目标文档', title.includes(QUERY.slice(0, 2)), title)

  await evaluate(`document.querySelector('.hit .icon-btn[aria-label="只读预览"]').click(); true`)
  await waitFor('预览抽屉打开', async () =>
    await evaluate(`!!document.querySelector('.pv-text')`))
  // 底部折叠条出现即代表 images 元数据已进 DOM（正文前 80 行可能一张图都没有）
  await waitFor('预览渲染完成', async () =>
    await evaluate(`!!document.querySelector('.pv-img-toggle, .pv-fig, .pv-inline-img')`))
  await sleep(600)

  // 首屏没有图（正文前 80 行全是文字）时展开全文，确保锚点被渲染
  let anchored = await evaluate(
    `document.querySelectorAll('.pv-fig, .pv-inline-img, .pv-cell-img').length`)
  if (anchored === 0) {
    await evaluate(`[...document.querySelectorAll('.pv-more button')][0]?.click(); true`)
    await waitFor('展开全文', async () =>
      await evaluate(`document.querySelectorAll('.pv-fig, .pv-inline-img, .pv-cell-img').length > 0`))
    console.log('首屏无图，已展开全文后复检')
    await sleep(800)
  }

  // 3) 三种形态的 DOM 断言
  const counts = await evaluate(`JSON.stringify({
    fig: document.querySelectorAll('.pv-fig').length,
    figImg: document.querySelectorAll('.pv-fig > img').length,
    inline: document.querySelectorAll('.pv-inline-img').length,
    cell: document.querySelectorAll('.pv-cell-img').length,
    chip: document.querySelectorAll('.pv-chip').length,
    toggle: document.querySelectorAll('.pv-img-toggle').length,
    grid: document.querySelectorAll('.pv-grid').length,
  })`)
  const c = JSON.parse(counts)
  console.log('DOM 计数：' + counts)
  const shapeHtml = await evaluate(
    `document.querySelector('.pv-text').innerHTML.replace(/<img[^>]*src="([^"]*)"[^>]*>/g, '<img src="$1">').slice(0, 700)`)
  console.log('正文首段 HTML：\n' + shapeHtml.replace(/></g, '>\n<'))
  check('出现块级原位图', c.fig > 0, `figure ${c.fig} / img ${c.figImg}`)
  check('不再渲染「图 N」徽标', c.chip === 0, `${c.chip} 个`)
  check('底部网格默认收起', c.toggle === 1 && c.grid === 0, `toggle ${c.toggle} / grid ${c.grid}`)

  // 滚到第一张原位图，等懒加载解码，再核对视口内的图
  await evaluate(`(() => {
    const f = document.querySelector('.pv-fig')
    if (f) f.scrollIntoView({ block: 'center' })
    return true
  })()`)
  await sleep(1500)
  const probeView = () => evaluate(`JSON.stringify((() => {
    const imgs = [...document.querySelectorAll('.pv-fig > img, .pv-inline-img, .pv-cell-img')]
    const vis = imgs.filter(i => {
      const r = i.getBoundingClientRect()
      return r.width > 0 && r.bottom > 0 && r.top < innerHeight
    })
    return {
      total: imgs.length,
      vis: vis.length,
      okInView: vis.filter(i => i.complete && i.naturalWidth > 0).length,
      broken: imgs.filter(i => i.complete && i.naturalWidth === 0).length,
    }
  })())`)
  // 大图解码要时间，轮询到视口内全部就绪（上限 15s）
  try {
    await waitFor('视口内图片解码', async () => {
      const v = JSON.parse(await probeView())
      return v.vis > 0 && v.okInView === v.vis
    }, 15000)
  } catch { /* 交给下面的断言报详情 */ }
  const view = JSON.parse(await probeView())
  check('视口内图片全部解码', view.vis > 0 && view.okInView === view.vis,
        JSON.stringify(view))
  check('全文档无裂图', view.broken === 0, `${view.broken} 张`)

  // 4) 展开底部网格
  await evaluate(`document.querySelector('.pv-img-toggle').click(); true`)
  await sleep(400)
  const gridAfter = await evaluate(`document.querySelectorAll('.pv-grid .pv-thumb').length`)
  check('点击后网格展开', gridAfter > 0, `${gridAfter} 张缩略图`)
  await evaluate(`document.querySelector('.pv-img-toggle').click(); true`)
  await sleep(300)

  await evaluate(`(() => {
    document.querySelectorAll('.n-scrollbar-container').forEach(e => e.scrollTo(0, 0))
    return true
  })()`)
  await evaluate(`(() => {
    const f = document.querySelector('.pv-fig')
    if (f) f.scrollIntoView({ block: 'center' })
    return true
  })()`)
  await sleep(1200)
  mkdirSync(join(ROOT, 'docs', 'screenshots'), { recursive: true })
  const shot = await cdp('Page.captureScreenshot', { format: 'png' })
  writeFileSync(SHOT, Buffer.from(shot.data, 'base64'))
  console.log(`截图：${SHOT}`)

  const pass = results.filter(Boolean).length
  console.log(`\n${pass}/${results.length} 通过`)
  if (pass !== results.length) process.exitCode = 1
}

function cleanup(code) {
  try { chrome?.kill() } catch { /* ignore */ }
  try { backend?.kill() } catch { /* ignore */ }
  process.exit(code)
}

main().then(() => cleanup(process.exitCode ?? 0))
  .catch(e => { console.error('ERROR', e.message); cleanup(1) })
