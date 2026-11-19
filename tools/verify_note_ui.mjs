#!/usr/bin/env node
/**
 * 记事本界面精简回归（CDP，零第三方依赖）。
 *
 * 覆盖：笔记本下拉切换只显示当前本分区；分区双击就地改名；分区 / 页面右键
 * 弹删除确认框（取消不删数据）；图片选中后显示边框。产出 08-note-ui.png。
 *
 * 用法：node tools/verify_note_ui.mjs
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
const SHOT = join(ROOT, 'docs', 'screenshots', '08-note-ui.png')
const TOKEN = 'note-ui-verify-20261116'
const PREFIX = '界面验证-'

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
  if (r.exceptionDetails)
    throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text)
  return r.result.value
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_ui_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_uichrome_'))
  const port = await freePort()
  const chromePort = await freePort()
  const base = `http://127.0.0.1:${port}`

  backend = spawn('py', ['-3', join(ROOT, 'backend', 'run.py'),
                         '--no-window', '--port', String(port)], {
    cwd: join(ROOT, 'backend'),
    env: { ...process.env, ONLYONE_TOKEN: TOKEN, ONLYONE_DATA_DIR: dataDir },
    stdio: 'ignore',
  })
  await waitFor('后端就绪', async () => (await fetch(base + '/api/meta/status')).ok)

  async function api(path, method = 'GET', body) {
    const r = await fetch(base + path, {
      method,
      headers: { 'X-OnlyOne-Token': TOKEN, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (!r.ok) throw new Error(`${path} ${r.status} ${await r.text()}`)
    return r.json()
  }

  // 两个笔记本各一个分区与页面，用于验证下拉切换只显示当前本
  const books = []
  const notes = []
  for (let i = 1; i <= 2; i++) {
    const b = await api('/api/notebooks', 'POST', { name: `${PREFIX}笔记本${i}` })
    const s = await api('/api/sections', 'POST',
                        { notebook_id: b.id, name: `${PREFIX}分区${i}` })
    const n = await api('/api/notes', 'POST',
                        { section_id: s.id, title: `${PREFIX}页面${i}` })
    books.push(b); notes.push({ ...n, section: s })
  }

  chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--remote-allow-origins=*',
    `--remote-debugging-port=${chromePort}`,
    `--user-data-dir=${profileDir}`,
    '--window-size=1440,900', 'about:blank',
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
            { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

  const url = `${base}/?token=${TOKEN}&tab=notes&note=${notes[0].id}`
  await cdp('Page.navigate', { url })
  await waitFor('页面加载', async () => await evaluate(`document.readyState === 'complete'`))
  await evaluate(`localStorage.setItem('onlyone_setup_skipped', '1'); true`)
  await cdp('Page.reload', {})
  await waitFor('笔记编辑器就绪', async () =>
    await evaluate(`!!document.querySelector('.tiptap-body') && document.readyState === 'complete'`))

  // 1) 笔记本下拉：只渲染当前本的「分区页签」
  const sectionNames = () => evaluate(
    `JSON.stringify([...document.querySelectorAll('.tabs .tab:not(.tab-more) .tab-name')].map(e => e.textContent.trim()))`)
  let names = JSON.parse(await sectionNames())
  check('默认显示第一本分区', names.length === 1 && names[0].includes('分区1'), JSON.stringify(names))

  await evaluate(`document.querySelector('.n-base-selection').click(); true`)
  await sleep(300)
  const optionCount = await evaluate(
    `document.querySelectorAll('.n-base-select-option').length`)
  check('下拉列出全部笔记本', optionCount === 2, `options=${optionCount}`)
  await evaluate(`[...document.querySelectorAll('.n-base-select-option')][1].click(); true`)
  await sleep(400)
  names = JSON.parse(await sectionNames())
  check('切换后只显示第二本分区', names.length === 1 && names[0].includes('分区2'), JSON.stringify(names))

  await evaluate(`document.querySelector('.n-base-selection').click(); true`)
  await sleep(250)
  await evaluate(`[...document.querySelectorAll('.n-base-select-option')][0].click(); true`)
  await sleep(400)

  // 2) 分区页签双击就地改名：Enter 提交，后端同步
  await evaluate(`(() => {
    const el = document.querySelector('.tabs .tab:not(.tab-more)');
    el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return true;
  })()`)
  await sleep(250)
  const hasInput = await evaluate(`!!document.querySelector('.tab-rename')`)
  check('双击分区页签出现就地输入框', hasInput)
  await evaluate(`(() => {
    const el = document.querySelector('.tab-rename');
    el.value = '${PREFIX}改名后分区';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`)
  await sleep(150)
  await evaluate(`(() => {
    const el = document.querySelector('.tab-rename');
    el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));
    return true;
  })()`)
  await sleep(600)
  const tree = await api('/api/notebooks')
  const renamed = tree[0].sections.some(s => s.name === `${PREFIX}改名后分区`)
  check('就地改名同步到后端', renamed)

  // 3) 分区 / 页面右键均弹删除确认框；取消后数据仍在
  async function rightClickAndCheck(selector, label) {
    await evaluate(`(() => {
      const el = document.querySelector('${selector}');
      el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
      return true;
    })()`)
    await sleep(300)
    const dialogShown = await evaluate(
      `document.body.innerText.includes('确认删除') && !!document.querySelector('.n-dialog')`)
    check(label, dialogShown)
    await evaluate(`(() => {
      const btn = [...document.querySelectorAll('.n-dialog button, .n-card button')]
        .find(b => b.textContent.trim() === '取消');
      btn && btn.click();
      return true;
    })()`)
    await sleep(300)
  }
  await rightClickAndCheck('.tabs .tab:not(.tab-more)', '分区页签右键弹确认框')
  await rightClickAndCheck('.note-row', '页面右键弹确认框')
  const treeAfter = await api('/api/notebooks')
  check('取消右键删除后数据未丢失',
        treeAfter[0].sections.length === 1 && treeAfter[0].sections[0].notes.length === 1)

  // 4) 图片选中显示边框：粘贴一张图后用真实鼠标点击
  //    用 canvas 生成 PNG 字节再贴，走和上一轮相同的位图链路
  //    切换笔记本会关闭当前页面（新布局行为），先把页面重新打开
  await evaluate(`(() => {
    const row = document.querySelector('.page-list .note-row');
    if (row) row.click();
    return true;
  })()`)
  await sleep(800)
  await evaluate(`(async () => {
    const el = document.querySelector('.tiptap-body');
    el.focus();
    const c = document.createElement('canvas'); c.width = 120; c.height = 60;
    const x = c.getContext('2d');
    x.fillStyle = '#173a63'; x.fillRect(0, 0, 120, 60);
    x.fillStyle = '#f08a4b'; x.font = 'bold 22px sans-serif'; x.fillText('SELECT', 12, 38);
    const blob = await new Promise(r => c.toBlob(r, 'image/png'));
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], 'ui.png', { type: 'image/png' }));
    el.dispatchEvent(new ClipboardEvent('paste',
      { clipboardData: dt, bubbles: true, cancelable: true }));
    return true;
  })()`, true)
  await sleep(3000)
  check('图片已插入编辑器',
        await evaluate(`!!document.querySelector('.tiptap-body img')`))

  const rect = JSON.parse(await evaluate(`(() => {
    const r = document.querySelector('.tiptap-body img').getBoundingClientRect();
    return JSON.stringify({ x: r.x + r.width / 2, y: r.y + r.height / 2 });
  })()`))
  await cdp('Input.dispatchMouseEvent',
            { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 })
  await cdp('Input.dispatchMouseEvent',
            { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 })
  await sleep(400)
  const selection = JSON.parse(await evaluate(`(() => {
    const img = document.querySelector('.tiptap-body img');
    const cs = getComputedStyle(img);
    return JSON.stringify({ selected: img.classList.contains('ProseMirror-selectednode'),
                            outline: cs.outlineWidth, color: cs.outlineColor });
  })()`))
  check('图片选中后显示边框',
        selection.selected && selection.outline !== '0px', JSON.stringify(selection))

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
