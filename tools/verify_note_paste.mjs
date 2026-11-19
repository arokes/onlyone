#!/usr/bin/env node
/**
 * 笔记「Ctrl+V 粘贴图片」真实浏览器回归（CDP，零第三方依赖）。
 *
 * 覆盖：合成 ClipboardEvent 注入位图 → 前端接管 → 上传 → 插入本地附件 URL
 * → 防抖自动保存；重复粘贴同图命中后端页内去重；图文混排粘贴保留文字、
 * 剥离外链图（CORS 降级忽略）。产出真实界面截图 07-note-paste.png。
 *
 * 用法：node tools/verify_note_paste.mjs
 * 前置：npm run build 已生成 frontend/dist；本机 Chrome 路径见 CHROME。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const SHOT = join(ROOT, 'docs', 'screenshots', '07-note-paste.png')
const TOKEN = 'note-paste-verify-20261116'

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
async function waitFor(label, probe, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { if (await probe()) return true } catch { /* 尚未就绪 */ }
    await sleep(250)
  }
  throw new Error(`${label} 超时`)
}

let backend, chrome, ws, dataDir, profileDir
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
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text)
  return r.result.value
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_paste_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_chrome_'))
  const port = await freePort()
  const chromePort = await freePort()
  const base = `http://127.0.0.1:${port}`

  backend = spawn('py', ['-3', join(ROOT, 'backend', 'run.py'),
                         '--no-window', '--port', String(port)], {
    cwd: join(ROOT, 'backend'),
    env: { ...process.env, ONLYONE_TOKEN: TOKEN, ONLYONE_DATA_DIR: dataDir },
    stdio: 'ignore',
  })
  await waitFor('后端就绪', async () =>
    (await fetch(base + '/api/meta/status')).ok)

  async function api(path, method = 'GET', body) {
    const r = await fetch(base + path, {
      method,
      headers: { 'X-OnlyOne-Token': TOKEN, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (!r.ok) throw new Error(`${path} ${r.status} ${await r.text()}`)
    return r.json()
  }

  const book = await api('/api/notebooks', 'POST', { name: '粘贴验证-笔记本' })
  const section = await api('/api/sections', 'POST',
                            { notebook_id: book.id, name: '分区' })
  const note = await api('/api/notes', 'POST',
                         { section_id: section.id, title: '粘贴验证页面' })

  chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--remote-allow-origins=*',
    `--remote-debugging-port=${chromePort}`,
    `--user-data-dir=${profileDir}`,
    '--window-size=1440,900', 'about:blank',
  ], { stdio: 'ignore' })
  const devtools = `http://127.0.0.1:${chromePort}`
  await waitFor('Chrome DevTools 就绪', async () =>
    (await fetch(devtools + '/json/version')).ok)
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
            { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

  const url = `${base}/?token=${TOKEN}&tab=notes&note=${note.id}`
  await cdp('Page.navigate', { url })
  await waitFor('页面加载', async () =>
    await evaluate(`document.readyState === 'complete'`))
  // 临时数据目录没有索引根目录，会弹「首次启动向导」遮住编辑器；标记跳过并重载
  await evaluate(`localStorage.setItem('onlyone_setup_skipped', '1'); true`)
  await cdp('Page.reload', {})
  await waitFor('笔记编辑器就绪', async () =>
    await evaluate(`!!document.querySelector('.tiptap-body') && document.readyState === 'complete'`))

  // 1) 图文混排粘贴（先在空文档上做）：文字保留，外链图被剥离并降级忽略。
  //    放在位图粘贴之前，避免默认粘贴替换掉选中的图片节点（编辑器正常语义）。
  await evaluate(`(() => {
    const el = document.querySelector('.tiptap-body');
    el.focus();
    const dt = new DataTransfer();
    dt.setData('text/plain', '外链粘贴文字保留XYZ');
    dt.setData('text/html', '<p>外链粘贴文字保留XYZ</p><img src="https://127.0.0.1:1/blocked.png">');
    el.dispatchEvent(new ClipboardEvent('paste',
      { clipboardData: dt, bubbles: true, cancelable: true }));
    return true;
  })()`)
  await sleep(4000)   // 等降级提示 toast 自动消失，让截图干净
  const mixed = JSON.parse(await evaluate(`JSON.stringify({
    text: document.querySelector('.tiptap-body').innerText,
    external: [...document.querySelectorAll('.tiptap-body img')]
      .filter(i => !/^\\/api\\/attachments\\//.test(i.getAttribute('src') || '')).length
  })`))
  check('图文混排文字保留', mixed.text.includes('外链粘贴文字保留XYZ'))
  check('外链图未进入正文', mixed.external === 0, `external=${mixed.external}`)

  // 2) 连续两次粘贴同一张位图：验证接管、插入、自动保存与页内去重
  const pasteCase = `(async () => {
    const el = document.querySelector('.tiptap-body');
    el.focus();
    const canvas = document.createElement('canvas');
    canvas.width = 96; canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#173a63'; ctx.fillRect(0, 0, 96, 64);
    ctx.fillStyle = '#f08a4b'; ctx.font = 'bold 26px sans-serif';
    ctx.fillText('PASTE', 8, 40);
    const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let prevented = 0;
    for (let i = 0; i < 2; i++) {
      const file = new File([bytes], 'pasted.png', { type: 'image/png' });
      const dt = new DataTransfer();
      dt.items.add(file);
      const ev = new ClipboardEvent('paste',
        { clipboardData: dt, bubbles: true, cancelable: true });
      el.dispatchEvent(ev);
      if (ev.defaultPrevented) prevented++;
    }
    return { prevented, bytes: bytes.length };
  })()`
  const paste = await evaluate(pasteCase, true)
  check('paste 事件被前端接管（2/2）', paste.prevented === 2, JSON.stringify(paste))

  await sleep(3000)   // 等串行上传 + 1s 防抖自动保存
  const domImgs = await evaluate(
    `JSON.stringify([...document.querySelectorAll('.tiptap-body img')].map(i => i.getAttribute('src')))`)
  const imgs = JSON.parse(domImgs)
  check('编辑器插入 2 张本地附件图',
        imgs.length === 2 && imgs.every(s => s && s.startsWith('/api/attachments/')),
        JSON.stringify(imgs))
  check('重复图片命中同一 URL', imgs.length === 2 && imgs[0] === imgs[1])

  const saved = await api(`/api/notes/${note.id}`)
  const doc = JSON.parse(saved.content_json || '{"content":[]}')
  const imageNodes = (doc.content || []).filter(n => n.type === 'image')
  check('自动保存后 content_json 含 2 个 image 节点', imageNodes.length === 2,
        `revision=${saved.revision}`)

  const attachDir = join(dataDir, 'attachments', String(note.id))
  const { readdirSync } = await import('node:fs')
  const files = readdirSync(attachDir).filter(f => /\.(png|jpg|gif|webp)$/.test(f))
  check('页内去重后附件目录仅 1 个文件', files.length === 1, JSON.stringify(files))

  mkdirSync(join(ROOT, 'docs', 'screenshots'), { recursive: true })
  const shot = await cdp('Page.captureScreenshot', { format: 'png' })
  writeFileSync(SHOT, Buffer.from(shot.data, 'base64'))
  check('真实界面截图已产出', existsSync(SHOT), SHOT)

  const ok = results.every(Boolean)
  console.log(`${ok ? 'ALL PASS' : 'SOME FAILED'}  (${results.filter(Boolean).length}/${results.length})`)
  return ok ? 0 : 1
}

main().then(code => { process.exitCode = code }).catch(e => {
  console.log('FAIL ' + (e && e.message ? e.message : String(e)))
  process.exitCode = 1
}).finally(async () => {
  try { ws && ws.close() } catch { /* ignore */ }
  for (const p of [chrome, backend]) {
    try { p && p.kill() } catch { /* ignore */ }
  }
  await sleep(400)
  for (const d of [profileDir, dataDir]) {
    try { d && rmSync(d, { recursive: true, force: true }) } catch { /* ignore */ }
  }
})
