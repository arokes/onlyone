#!/usr/bin/env node
/**
 * 记事本「切页签再回来」的状态恢复回归（CDP，零第三方依赖）。
 *
 * 覆盖：
 *  1. 选中第 2 个分区 + 第 2 个分区的页面 → 切到运维记录 → 切回记事本，
 *     分区页签与页面仍是原来那个；
 *  2. 编辑后 1 秒内切走（防抖保存尚未触发）→ 内容仍落盘，回来能看到。
 *
 * 用法：node tools/verify_note_restore.mjs
 * 前置：npm run build 已生成 frontend/dist。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const TOKEN = 'note-restore-20261124'
const BOOK = '恢复验证'
const SHOT = join(ROOT, 'docs', 'screenshots', '22-notes-restore.png')

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

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_restore_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_restorechrome_'))
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

  // 建一个空索引目录：否则首次启动向导会盖住界面（截图与点击都会受影响）
  const idxDir = join(dataDir, 'idx')
  mkdirSync(idxDir, { recursive: true })
  await api('/api/roots', 'POST', { path: idxDir, kind: 'local' })

  const book = await api('/api/notebooks', 'POST', { name: BOOK })
  const sectionA = await api('/api/sections', 'POST', { notebook_id: book.id, name: '分区甲' })
  const sectionB = await api('/api/sections', 'POST', { notebook_id: book.id, name: '分区乙' })
  const noteA = await api('/api/notes', 'POST', { section_id: sectionA.id, title: '甲页面' })
  const noteB = await api('/api/notes', 'POST', { section_id: sectionB.id, title: '乙页面' })
  for (const [nid, text] of [[noteA.id, '甲的内容'], [noteB.id, '乙的内容']]) {
    await api(`/api/notes/${nid}`, 'PUT', {
      title: nid === noteA.id ? '甲页面' : '乙页面',
      content_json: JSON.stringify({ type: 'doc', content: [
        { type: 'paragraph', content: [{ type: 'text', text }] },
      ] }),
      is_private: false, revision: 0,
    })
  }

  chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--remote-allow-origins=*',
    `--remote-debugging-port=${chromePort}`, `--user-data-dir=${profileDir}`,
    '--window-size=1440,900', 'about:blank',
  ], { stdio: 'ignore' })
  const devtools = `http://127.0.0.1:${chromePort}`
  await waitFor('Chrome DevTools 就绪', async () => (await fetch(devtools + '/json/version')).ok)
  const targets = await (await fetch(devtools + '/json/list')).json()
  const page = targets.find(t => t.type === 'page')
  if (!page) throw new Error('没有可用的 Chrome page target')

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

  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=notes` })
  await waitFor('记事本就绪', async () => await evaluate(
    `!!document.querySelector('.notes-layout') && !!document.querySelector('.note-row')`))
  await sleep(500)

  const goto = async label => {
    await evaluate(`(() => {
      const btn = [...document.querySelectorAll('.nav-item')]
        .find(b => b.textContent.trim() === '${label}');
      if (!btn) return 'no-nav';
      btn.click();
      return 'ok';
    })()`)
    await sleep(600)
  }

  // --- 1) 选中「分区乙 / 乙页面」---
  await evaluate(`(() => {
    const tab = [...document.querySelectorAll('.tabs .tab')]
      .find(t => t.textContent.trim() === '分区乙');
    if (!tab) return 'no-tab';
    tab.click();
    return 'ok';
  })()`)
  await sleep(400)
  await evaluate(`(() => {
    const row = [...document.querySelectorAll('.note-row')]
      .find(r => r.textContent.includes('乙页面'));
    if (!row) return 'no-row';
    row.click();
    return 'ok';
  })()`)
  await waitFor('乙页面载入', async () => await evaluate(
    `document.querySelector('.note-row.active')?.textContent.includes('乙页面') === true`))

  // --- 2) 切走再切回 ---
  await goto('运维记录')
  const leftNotes = await evaluate(`!document.querySelector('.notes-layout')`)
  check('切到其它页签后记事本已卸载', leftNotes === true)

  await goto('记事本')
  await waitFor('记事本重新挂载', async () => await evaluate(`!!document.querySelector('.notes-layout')`))
  await sleep(900)

  const restored = await evaluate(`(() => {
    const tab = document.querySelector('.tabs .tab.active');
    const row = document.querySelector('.note-row.active');
    const title = document.querySelector('.notes-layout input[placeholder="页面标题"]');
    return JSON.stringify({
      tab: tab ? tab.textContent.trim() : '',
      row: row ? row.textContent.trim() : '',
      title: title ? title.value : '',
      rows: document.querySelectorAll('.note-row').length,
    });
  })()`)
  const r = JSON.parse(restored)
  check('回到记事本仍在原分区页签', r.tab === '分区乙', `tab=${r.tab}`)
  check('回到记事本仍选中原页面', r.row.includes('乙页面'), `row=${r.row}`)
  check('编辑器载入的是原页面', r.title === '乙页面', `title=${r.title}`)
  check('左侧列表是原分区的页面', r.rows === 1, `rows=${r.rows}`)

  // --- 3) 防抖未触发就切走，内容不能丢 ---
  // 用 execCommand 写入：它会触发 beforeinput/input，TipTap 才会走 onUpdate → markDirty。
  // CDP 的 Input.insertText 在 headless 下没能把字符送进 ProseMirror（实测编辑器内容未变）。
  const typedOk = await evaluate(`(() => {
    const pm = document.querySelector('.tiptap .ProseMirror');
    if (!pm) return 'no-editor';
    pm.focus();
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(pm);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
    return document.execCommand('insertText', false, '切页签前刚敲的字') ? 'ok' : 'exec-failed';
  })()`)
  check('测试文本已写入编辑器', typedOk === 'ok', String(typedOk))
  await sleep(150)                      // 远小于 1s 防抖
  await goto('运维记录')
  await sleep(1600)                     // 等卸载前那次保存返回

  const saved = await api(`/api/notes/${noteB.id}`)
  check('切页签时未落盘的编辑已保存',
        String(saved.content_json).includes('切页签前刚敲的字'),
        saved.content_json.slice(0, 60))

  await goto('记事本')
  await waitFor('记事本再次挂载', async () => await evaluate(`!!document.querySelector('.notes-layout')`))
  await sleep(900)
  const text = await evaluate(
    `document.querySelector('.tiptap .ProseMirror')?.innerText || ''`)
  check('回到记事本能看到刚敲的内容', text.includes('切页签前刚敲的字'), text.slice(0, 40))

  const shot = await cdp('Page.captureScreenshot', { format: 'png' })
  if (shot.data) {
    writeFileSync(SHOT, Buffer.from(shot.data, 'base64'))
    check('截图产出', true, SHOT)
  } else {
    check('截图产出', false, 'captureScreenshot 无数据')
  }

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
