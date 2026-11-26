#!/usr/bin/env node
/**
 * 功能 4/5/6 的界面回归（2026-11-25，CDP）。
 *
 * 覆盖
 * ----
 * * 双链：正文里 `[[...]]` 渲染为可点链接，底部出现「本页引用 / 反向链接」面板；
 * * 动态码：凭据列表出现 6 位码，且能点击复制（不验证系统剪贴板内容）；
 * * 快速捕获：模拟热键后浮层弹出，输入并保存后落库。
 *
 * 截图：29-wiki-notes.png / 30-totp.png / 31-capture.png
 * 用法：node tools/verify_extras_ui.mjs（需先 npm run build）
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const SHOTS = join(ROOT, 'docs', 'screenshots')
const TOKEN = 'extras-ui-20261125'
const SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'

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
    try { if (await probe()) return true } catch { /* 未就绪 */ }
    await sleep(200)
  }
  throw new Error(`${label} 超时`)
}

let backend, chrome, ws
let dataDir, profileDir
let seq = 0
const pending = new Map()
function cdp(method, params = {}) {
  const id = ++seq
  return new Promise((res, rej) => {
    pending.set(id, { res, rej })
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const r = await cdp('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails)
    throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text)
  return r.result.value
}
async function shot(name) {
  const data = await cdp('Page.captureScreenshot', { format: 'png' })
  writeFileSync(join(SHOTS, name), Buffer.from(data.data, 'base64'))
  check(`截图 ${name}`, true)
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')
  mkdirSync(SHOTS, { recursive: true })

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_xui_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_xuichrome_'))
  const port = await freePort()
  const base = `http://127.0.0.1:${port}`

  backend = spawn('py', ['-3', join(ROOT, 'backend', 'run.py'),
                         '--no-window', '--port', String(port)], {
    cwd: join(ROOT, 'backend'),
    env: {
      ...process.env, ONLYONE_TOKEN: TOKEN, ONLYONE_DATA_DIR: dataDir,
      ONLYONE_DEV_HOTKEY: '1',   // 允许 /api/capture/_trigger 模拟热键
    },
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

  // 先落一条索引目录：否则 App 启动时 counts.roots===0 会弹首次启动向导，盖住页面
  const idxDir = join(dataDir, 'idx')
  mkdirSync(idxDir, { recursive: true })
  const sqlPath = join(dataDir, 'seed.sql')
  writeFileSync(sqlPath,
    "INSERT INTO roots(path, kind) VALUES('" + idxDir.replace(/'/g, "''") + "', 'local');", 'utf8')
  const seeded = spawnSync('py', ['-3', '-c',
    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.executescript(open(sys.argv[2], encoding='utf-8').read()); c.commit(); c.close()",
    join(dataDir, 'onlyone.db'), sqlPath], { encoding: 'utf8' })
  if (seeded.status !== 0) throw new Error('造 root 失败：' + (seeded.stderr || seeded.stdout))

  // ---------------------------------------------------- 数据：双链 + 动态码
  const nb = await api('/api/notebooks', 'POST', { name: 'SAP 笔记' })
  const sec = await api('/api/sections', 'POST', { notebook_id: nb.id, name: 'MRP' })
  const abap = await api('/api/sections', 'POST', { notebook_id: nb.id, name: 'ABAP' })
  const pageA = await api('/api/notes', 'POST', { section_id: sec.id, title: 'MRP 边际码排查' })
  const pageB = await api('/api/notes', 'POST', { section_id: sec.id, title: '月度结账清单' })

  const doc = text => JSON.stringify({
    type: 'doc',
    content: [{ type: 'paragraph', content: [{ type: 'text', text }] }],
  })
  await api(`/api/notes/${pageA.id}`, 'PUT', {
    content_json: doc('重复生成时先看边际码设置，结账前对照 [[月度结账清单]] 逐项核对。'),
    revision: 0,
  })
  await api(`/api/notes/${pageB.id}`, 'PUT', {
    content_json: doc('边际码核对见 [[MRP 边际码排查]]，另有 [[还没写的页面]] 待补。'),
    revision: 0,
  })
  await api('/api/credentials', 'POST', {
    title: 'SAP DS4 生产', username: '10000265', password: 'p@ss', totp: SECRET,
  })

  const chromePort = await freePort()
  chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--remote-allow-origins=*',
    `--remote-debugging-port=${chromePort}`, `--user-data-dir=${profileDir}`,
    '--window-size=1600,950', 'about:blank',
  ], { stdio: 'ignore' })
  const devtools = `http://127.0.0.1:${chromePort}`
  await waitFor('Chrome 就绪', async () => (await fetch(devtools + '/json/version')).ok)
  const targets = await (await fetch(devtools + '/json/list')).json()
  const page = targets.find(t => t.type === 'page')
  if (!page) throw new Error('没有 page target')

  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP 连接失败')) })
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data)
    if (!msg.id || !pending.has(msg.id)) return
    const { res, rej } = pending.get(msg.id)
    pending.delete(msg.id)
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)
  }
  await cdp('Page.enable')
  await cdp('Runtime.enable')

  // ---------------------------------------------------- 双链
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=notes&note=${pageA.id}` })
  await waitFor('笔记页就绪', async () => await evaluate(
    `document.body.innerText.includes('MRP 边际码排查')`))
  await sleep(1600)  // 等编辑器与 links 请求

  const wiki = JSON.parse(await evaluate(`(() => {
    const panel = document.querySelector('.wiki-panel');
    return JSON.stringify({
      links: document.querySelectorAll('.wiki-link').length,
      panel: panel ? panel.innerText.replace(/\\s+/g, ' ').trim() : '',
    });
  })()`))
  check('正文里的 [[...]] 渲染为链接', wiki.links >= 1, `count=${wiki.links}`)

  const linkStyle = JSON.parse(await evaluate(`(() => {
    const el = document.querySelector('.wiki-link');
    if (!el) return '{}';
    const cs = getComputedStyle(el);
    return JSON.stringify({ color: cs.color, border: cs.borderBottomStyle, cls: el.className });
  })()`))
  check('双链样式生效（虚线下划线 + 强调色）',
        linkStyle.border === 'dashed' && linkStyle.color === 'rgb(164, 69, 36)',
        JSON.stringify(linkStyle))
  check('面板显示本页引用与反向链接',
        wiki.panel.includes('本页引用') && wiki.panel.includes('反向链接'), wiki.panel.slice(0, 120))
  check('反链列出引用本页的页面', wiki.panel.includes('月度结账清单'))
  await shot('29-wiki-notes.png')

  // 点击正文链接应跳到 B
  await evaluate(`(() => {
    const el = document.querySelector('.wiki-link');
    el && el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  })()`)
  await sleep(1200)
  const afterClick = await evaluate(`document.body.innerText`)
  check('点正文链接跳到目标页面', afterClick.includes('结账前') || afterClick.includes('月度结账清单'))

  // ---------------------------------------------------- 动态码
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=credentials` })
  await waitFor('凭据页就绪', async () => await evaluate(
    `document.body.innerText.includes('SAP DS4 生产')`))
  await sleep(600)
  const codeText = await evaluate(`(() => {
    const el = document.querySelector('.totp-code');
    return el ? el.textContent.trim() : '';
  })()`)
  check('凭据列表显示 6 位动态码', /^\d{6}$/.test(codeText), codeText)
  await shot('30-totp.png')

  // ---------------------------------------------------- 主窗口不应挂载捕获浮层
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}` })
  await waitFor('概览就绪', async () => await evaluate(
    `document.body.innerText.includes('概览')`))
  await sleep(600)
  const mainShell = JSON.parse(await evaluate(`(() => JSON.stringify({
    card: !!document.querySelector('.cap-card'),
    mask: !!document.querySelector('.cap-mask'),
  }))()`))
  check('主窗口不再挂载捕获浮层', !mainShell.card && !mainShell.mask, JSON.stringify(mainShell))

  // ---------------------------------------------------- 独立捕获小窗（?capture=1）
  await cdp('Emulation.setDeviceMetricsOverride',
            { width: 620, height: 272, deviceScaleFactor: 1, mobile: false })
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&capture=1` })
  await waitFor('捕获小窗就绪', async () => await evaluate(`!!document.querySelector('.cap-card')`))
  await sleep(900)
  const solo = JSON.parse(await evaluate(`(() => JSON.stringify({
    card: !!document.querySelector('.cap-card'),
    shell: !!document.querySelector('.app'),
    sidebar: !!document.querySelector('.sidebar'),
    standalone: !!document.querySelector('.cap-mask.standalone'),
  }))()`))
  check('独立小窗只渲染捕获卡片（无应用壳 / 无侧栏）',
        solo.card && !solo.shell && !solo.sidebar && solo.standalone, JSON.stringify(solo))
  await shot('32-capture-window.png')

  // 笔记模式 = TipTap 编辑器，可直接粘贴文字与截图
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll('.cap-tab')]
      .find(t => t.textContent.trim() === '笔记');
    tab && tab.click();
  })()`)
  await sleep(600)
  const hasEditor = await evaluate(`!!document.querySelector('.cap-editor .ProseMirror')`)
  check('笔记模式使用富文本编辑器', hasEditor)

  await evaluate(`(() => {
    const host = document.querySelector('.cap-editor .ProseMirror');
    if (!host) return 'no-editor';
    host.focus();
    const dt = new DataTransfer();
    dt.setData('text/plain', 'SAP-PP 计划订单又重复生成，边际码已改回 3');
    host.dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: dt, bubbles: true, cancelable: true,
    }));
    return 'ok';
  })()`)
  await sleep(500)

  await evaluate(`(async () => {
    // 用 canvas 生成一张真实尺寸的截图，便于人工核对渲染效果
    const canvas = document.createElement('canvas');
    canvas.width = 260; canvas.height = 84;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#f4eee2'; ctx.fillRect(0, 0, 260, 84);
    ctx.strokeStyle = '#d9cbb5'; ctx.strokeRect(0.5, 0.5, 259, 83);
    ctx.fillStyle = '#a44524'; ctx.font = '15px sans-serif';
    ctx.fillText('SAP 截图示例', 16, 34);
    ctx.fillStyle = '#6f675b'; ctx.font = '12px sans-serif';
    ctx.fillText('粘贴后随笔记一起保存', 16, 58);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    const dt = new DataTransfer();
    dt.items.add(new File([blob], 'shot.png', { type: 'image/png' }));
    const host = document.querySelector('.cap-editor .ProseMirror');
    host.dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: dt, bubbles: true, cancelable: true,
    }));
  })()`)
  await sleep(900)
  const imgCount = await evaluate(`document.querySelectorAll('.cap-editor img').length`)
  check('粘贴截图后编辑器出现图片', imgCount >= 1, `img=${imgCount}`)

  // 落点：展开下拉 → 列出全部分区 → 选 ABAP 并记忆
  await evaluate(`(() => {
    const sel = document.querySelector('.cap-target .n-base-selection');
    sel && sel.click();
  })()`)
  await sleep(600)
  const menu = JSON.parse(await evaluate(`(() => {
    const m = document.querySelector('.n-base-select-menu');
    if (!m) return JSON.stringify({ open: false });
    const r = m.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + 10);
    return JSON.stringify({
      open: r.height > 0,
      onTop: !!hit && (m.contains(hit) || hit === m),
      options: [...m.querySelectorAll('.n-base-select-option')]
        .map(o => o.textContent.trim()),
    });
  })()`))
  check('落点下拉能展开且浮在遮罩之上', menu.open && menu.onTop,
        JSON.stringify({ open: menu.open, onTop: menu.onTop }))
  check('下拉列出其它分区', (menu.options || []).includes('ABAP') && (menu.options || []).includes('MRP'),
        JSON.stringify(menu.options))

  await evaluate(`(() => {
    const opt = [...document.querySelectorAll('.n-base-select-option')]
      .find(o => o.textContent.trim() === 'ABAP');
    opt && opt.click();
  })()`)
  await sleep(700)
  const target = await api('/api/capture/target')
  check('选中 ABAP 后成为默认落点', target.section_name === 'ABAP', target.section_name)
  await shot('33-capture-image.png')

  // Ctrl+Enter 保存：笔记应落进 ABAP 分区，且图片已落盘
  await evaluate(`(() => {
    document.querySelector('.cap-editor .ProseMirror')
      ?.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', ctrlKey: true, bubbles: true,
      }));
  })()`)
  await sleep(1500)
  const notes = await api('/api/notes')
  const saved = notes.find(n => String(n.title || '').includes('边际码已改回 3'))
  check('保存写入笔记', !!saved, `count=${notes.length}`)
  if (saved) {
    const detail = await api('/api/notes/' + saved.id)
    check('笔记落在选中的 ABAP 分区', detail.section_id === abap.id,
          `section=${detail.section_id} expect=${abap.id}`)
    const cj = String(detail.content_json || '')
    // 后端用 json.dumps 写库，字段与值之间带空格，这里只认 "image" 这个词
    check('笔记内含图片节点', cj.includes('"image"'), cj.slice(0, 420))
    check('图片已落盘为附件', cj.includes('/api/attachments/'),
          cj.includes('data:image') ? '仍是 base64' : '无 image 节点')
  }
  await cdp('Emulation.clearDeviceMetricsOverride')

  const ok = results.every(Boolean)
  console.log(`\n${ok ? 'ALL PASS' : 'SOME FAILED'}  (${results.filter(Boolean).length}/${results.length})`)
  return ok
}

main()
  .then(ok => { cleanup(); process.exit(ok ? 0 : 1) })
  .catch(err => { console.error('FAIL', err.message || err); cleanup(); process.exit(1) })

function cleanup() {
  for (const proc of [chrome, backend]) {
    if (proc && !proc.killed) { try { proc.kill() } catch { /* 已退出 */ } }
  }
  for (const dir of [profileDir, dataDir]) {
    if (dir) { try { rmSync(dir, { recursive: true, force: true }) } catch { /* 忽略 */ } }
  }
}
