#!/usr/bin/env node
/**
 * 记事本表格界面回归（CDP，零第三方依赖）。
 *
 * 覆盖：工具栏「表格」菜单 → 插入 3×3 → 单元格输入 → Shift 多选后合并 →
 * 自动保存后 content_json 落库 → 截图。
 *
 * 用法：node tools/verify_note_table_ui.mjs
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
const SHOT = join(ROOT, 'docs', 'screenshots', '09-notes-table.png')
const TOKEN = 'note-table-verify-20261118'
const PREFIX = '表格验证-'

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
const q = sel => `document.querySelector(${JSON.stringify(sel)})`

/** 点击工具栏「表格」并等下拉渲染；返回是否成功展开。 */
async function openTableMenu() {
  await evaluate(`(() => {
    const btn = [...document.querySelectorAll('.toolbar button')]
      .find(b => b.textContent.trim() === '表格');
    if (!btn) throw new Error('未找到表格按钮');
    btn.click();
    return true;
  })()`)
  await sleep(350)
  return await evaluate(`!!document.querySelector('.n-dropdown-menu')`)
}

/** 点下拉里文本精确匹配的项，返回该项是否存在。
 *  必须派发真实鼠标事件——naive-ui 的菜单项挂在内部节点上，
 *  元素级 .click() 不会触发它的 pointer 流程（实测无任何回调）。 */
async function pickMenuItem(text) {
  const rect = await evaluate(`(() => {
    const label = [...document.querySelectorAll('.n-dropdown-option-body__label')]
      .find(e => e.textContent.trim() === ${JSON.stringify(text)});
    if (!label) return null;
    const r = label.getBoundingClientRect();
    return JSON.stringify({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
  })()`)
  if (!rect) return false
  const { x, y } = JSON.parse(rect)
  await cdp('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y })
  await cdp('Input.dispatchMouseEvent',
            { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
  await cdp('Input.dispatchMouseEvent',
            { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
  await sleep(350)
  return true
}

/** 菜单项是否置灰 */
async function menuItemDisabled(text) {
  const shown = await openTableMenu()
  if (!shown) return null
  const disabled = await evaluate(`(() => {
    const label = [...document.querySelectorAll('.n-dropdown-option-body__label')]
      .find(e => e.textContent.trim() === ${JSON.stringify(text)});
    if (!label) return null;
    const opt = label.closest('.n-dropdown-option');
    return !!opt.querySelector('.n-dropdown-option-body--disabled') ||
      opt.className.includes('disabled');
  })()`)
  // ESC 关闭：点 body 在 headless 下不一定能让 naive-ui 收起菜单，残留会挡住后续点击
  for (const type of ['keyDown', 'keyUp']) {
    await cdp('Input.dispatchKeyEvent',
              { type, key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  }
  await sleep(300)
  return disabled
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_tbl_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_tblchrome_'))
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

  const book = await api('/api/notebooks', 'POST', { name: `${PREFIX}笔记本` })
  const section = await api('/api/sections', 'POST',
                            { notebook_id: book.id, name: `${PREFIX}分区` })
  const note = await api('/api/notes', 'POST',
                         { section_id: section.id, title: `${PREFIX}表格页` })

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

  const url = `${base}/?token=${TOKEN}&tab=notes&note=${note.id}`
  await cdp('Page.navigate', { url })
  await waitFor('页面加载', async () => await evaluate(`document.readyState === 'complete'`))
  await evaluate(`localStorage.setItem('onlyone_setup_skipped', '1'); true`)
  await cdp('Page.reload', {})
  await waitFor('笔记编辑器就绪', async () =>
    await evaluate(`!!document.querySelector('.tiptap-body') && document.readyState === 'complete'`))

  // 1) 光标不在表格内：行/列/合并类菜单项置灰
  const mergeDisabledBefore = await menuItemDisabled('合并单元格')
  check('未在表格内时合并单元格置灰', mergeDisabledBefore === true,
        `disabled=${mergeDisabledBefore}`)

  // 2) 插入 3×3 表格（注：TipTap 不生成 thead，表头首行是 tbody 里的 th）
  await openTableMenu()
  const inserted = await pickMenuItem('插入 3×3 表格')
  await sleep(400)
  const shape = await evaluate(`(() => {
    const t = document.querySelector('.tiptap-body table');
    if (!t) return null;
    const firstRow = t.querySelector('tr');
    return { rows: t.querySelectorAll('tr').length,
             cols: firstRow ? firstRow.children.length : 0,
             heads: t.querySelectorAll('tr:first-child th').length };
  })()`)
  check('菜单插入 3×3 表格', inserted && shape && shape.rows === 3 && shape.cols === 3,
        JSON.stringify(shape))
  check('首行渲染为表头 th', shape && shape.heads === 3, JSON.stringify(shape))

  // 3) 单元格输入：插入后光标在第一格
  await cdp('Input.insertText', { text: '库名' })
  await sleep(300)
  const firstCell = await evaluate(
    `document.querySelector('.tiptap-body table td, .tiptap-body table th').textContent.trim()`)
  check('单元格可输入文字', firstCell === '库名', `text=${firstCell}`)

  // 4) 在表格内：合并单元格可用
  const mergeDisabledInTable = await menuItemDisabled('合并单元格')
  check('表格内合并单元格可用', mergeDisabledInTable === false,
        `disabled=${mergeDisabledInTable}`)

  // 5) Shift 多选前两格后合并
  await evaluate(`(() => {
    const cells = [...document.querySelectorAll('.tiptap-body table th, .tiptap-body table td')];
    const a = cells[0].getBoundingClientRect(), b = cells[1].getBoundingClientRect();
    window.__cellA = { x: a.left + a.width / 2, y: a.top + a.height / 2 };
    window.__cellB = { x: b.left + b.width / 2, y: b.top + b.height / 2 };
    return true;
  })()`)
  const pts = await evaluate(`JSON.stringify({ a: window.__cellA, b: window.__cellB })`)
  const { a, b } = JSON.parse(pts)
  const click = async (p, modifiers = 0) => {
    await cdp('Input.dispatchMouseEvent',
              { type: 'mousePressed', x: p.x, y: p.y, button: 'left', clickCount: 1, modifiers })
    await cdp('Input.dispatchMouseEvent',
              { type: 'mouseReleased', x: p.x, y: p.y, button: 'left', clickCount: 1, modifiers })
    await sleep(200)
  }
  await click(a)              // 先落点到第一格
  await click(b, 8)           // Shift=8，扩展选到第二格
  await openTableMenu()
  await pickMenuItem('合并单元格')
  await sleep(400)
  const merged = await evaluate(`(() => {
    const cell = document.querySelector('.tiptap-body table th[colspan], .tiptap-body table td[colspan]');
    return cell ? Number(cell.getAttribute('colspan')) : 0;
  })()`)
  check('Shift 多选后合并成 colspan=2', merged === 2, `colspan=${merged}`)

  // 6) 自动保存落库：content_json 含 table 节点与合并属性
  const saved = await waitFor('表格自动保存', async () => {
    const d = await api(`/api/notes/${note.id}`)
    if (!d.content_json) return false
    const doc = JSON.parse(d.content_json)
    const table = (doc.content || []).find(n => n.type === 'table')
    if (!table) return false
    const json = JSON.stringify(table)
    return json.includes('"colspan":2') && json.includes('库名') ? table : false
  }, 12000).then(() => true).catch(() => false)
  check('自动保存后 content_json 含表格与合并且 revision 递增',
        saved && (await api(`/api/notes/${note.id}`)).revision >= 1)

  // 7) 导出的 Markdown 是合法 GFM 表格
  const mdResp = await fetch(
    `${base}/api/notes/export?scope=page&id=${note.id}&format=md`,
    { headers: { 'X-OnlyOne-Token': TOKEN } })
  const md = await mdResp.text()
  check('导出 Markdown 含 GFM 表格分隔行',
        md.includes('| --- | --- |') && md.includes('库名'), md.split('\n').slice(1, 4).join(' / '))

  // 8) 截图
  mkdirSync(join(ROOT, 'docs', 'screenshots'), { recursive: true })
  const shot = await cdp('Page.captureScreenshot', { format: 'png' })
  writeFileSync(SHOT, Buffer.from(shot.data, 'base64'))
  check('截图产出', existsSync(SHOT), SHOT)

  const ok = results.every(Boolean)
  console.log(`${ok ? 'ALL PASS' : 'SOME FAILED'}  (${results.filter(Boolean).length}/${results.length})`)
  return ok ? 0 : 1
}

try {
  process.exitCode = await main()
} catch (err) {
  console.error('ERROR', err.message)
  process.exitCode = 1
} finally {
  try { ws?.close() } catch { /* ignore */ }
  try { chrome?.kill() } catch { /* ignore */ }
  try { backend?.kill() } catch { /* ignore */ }
  await sleep(300)
  for (const dir of [dataDir, profileDir]) {
    if (dir) { try { rmSync(dir, { recursive: true, force: true }) } catch { /* ignore */ } }
  }
}
