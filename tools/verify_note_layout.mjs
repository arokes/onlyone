#!/usr/bin/env node
/**
 * 记事本新布局回归（CDP，零第三方依赖）。
 *
 * 覆盖：顶部页签固定（滚动正文不位移）· 左右两栏独立滚动 · 分区多时收进「更多」·
 * 切页签只换左侧列表 · 页签不带页数徽标。
 *
 * 用法：node tools/verify_note_layout.mjs
 * 前置：npm run build 已生成 frontend/dist。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const SHOT = join(ROOT, 'docs', 'screenshots', '16-notes-layout.png')
const TOKEN = 'note-layout-20261118'
const BOOK = '布局验证'
const SECTION_NAMES = ['随记', 'MM 采购与库存', 'ABAP 报表与增强', 'BASIC 基础配置',
                       'PP 生产计划', 'PP资料 归档', 'PS 项目系统', '帆软报表SQL 取数',
                       '晶盛PP配置说明', '接口文档清单', '权限对象与角色', '测试记录',
                       '归档 2024', '临时草稿', '主数据维护', '月结与年结', 'MES 对接',
                       'WMS 仓储']

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
async function evaluate(expression) {
  const r = await cdp('Runtime.evaluate', { expression, returnByValue: true })
  if (r.exceptionDetails)
    throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text)
  return r.result.value
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_lay_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_laychrome_'))
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

  // 14 个分区：第一个 30 页（撑出左侧滚动），其余 2 页
  const book = await api('/api/notebooks', 'POST', { name: BOOK })
  let firstNoteId = 0
  for (const [index, name] of SECTION_NAMES.entries()) {
    const section = await api('/api/sections', 'POST', { notebook_id: book.id, name })
    const pageCount = index === 0 ? 30 : 2
    for (let p = 0; p < pageCount; p++) {
      const note = await api('/api/notes', 'POST',
                             { section_id: section.id, title: `${name} 页面 ${p + 1}` })
      if (!firstNoteId) firstNoteId = note.id
      const content = Array.from({ length: 60 }, (_, k) => ({
        type: 'paragraph',
        content: [{ type: 'text', text: `第 ${k + 1} 段：${name} 的内容，用来把正文撑长，验证右侧独立滚动。` }],
      }))
      await api(`/api/notes/${note.id}`, 'PUT', {
        title: `${name} 页面 ${p + 1}`,
        content_json: JSON.stringify({ type: 'doc', content }),
        is_private: false, revision: 0,
      })
    }
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
  await cdp('Emulation.setDeviceMetricsOverride',
            { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=notes&note=${firstNoteId}` })
  await waitFor('页面加载', async () => await evaluate(`document.readyState === 'complete'`))
  await evaluate(`localStorage.setItem('onlyone_setup_skipped','1'); true`)
  await cdp('Page.reload')
  await waitFor('笔记就绪', async () =>
    await evaluate(`!!document.querySelector('.notes-layout') && !!document.querySelector('.tiptap')
                    && document.readyState === 'complete'`))
  await sleep(800)

  // 1) 三区结构
  const shape = await evaluate(`(() => {
    const q = s => document.querySelector(s);
    return {
      topbar: !!q('.notes-topbar'),
      tabs: !!q('.tabs'),
      pages: !!q('.pages'),
      list: !!q('.page-list'),
      editor: !!q('.tiptap'),
      tabCount: document.querySelectorAll('.tabs .tab:not(.tab-more)').length,
      more: !!q('.tab-more'),
      badges: document.querySelectorAll('.tabs .tab-count').length,
    };
  })()`)
  check('顶部固定条 + 左右两栏结构存在',
        shape.topbar && shape.tabs && shape.pages && shape.list && shape.editor,
        JSON.stringify(shape))
  check('页签不带页数徽标', shape.badges === 0, `badges=${shape.badges}`)
  check('分区多时收进「更多」', shape.more && shape.tabCount < SECTION_NAMES.length,
        `可见 ${shape.tabCount} / 共 ${SECTION_NAMES.length}`)

  // 2) 右侧滚动：左侧与顶部页签不动
  const before = await evaluate(`(() => {
    const t = document.querySelector('.tiptap');
    const l = document.querySelector('.page-list');
    const tabs = document.querySelector('.tabs');
    const content = document.querySelector('.content');
    return { tTop: t.scrollTop, lTop: l.scrollTop,
             tabsTop: Math.round(tabs.getBoundingClientRect().top),
             contentTop: content.scrollTop };
  })()`)
  await evaluate(`document.querySelector('.tiptap').scrollTop = 500; true`)
  await sleep(350)
  const after = await evaluate(`(() => {
    const t = document.querySelector('.tiptap');
    const l = document.querySelector('.page-list');
    const tabs = document.querySelector('.tabs');
    const content = document.querySelector('.content');
    return { tTop: t.scrollTop, lTop: l.scrollTop,
             tabsTop: Math.round(tabs.getBoundingClientRect().top),
             contentTop: content.scrollTop };
  })()`)
  check('右侧正文可独立滚动', after.tTop > 0, `scrollTop=${after.tTop}`)
  check('右侧滚动时左侧列表不动', after.lTop === before.lTop,
        `${before.lTop} → ${after.lTop}`)
  check('右侧滚动时顶部页签位置不变', after.tabsTop === before.tabsTop,
        `${before.tabsTop} → ${after.tabsTop}`)
  check('外层 .content 没有被带着滚', after.contentTop === 0, `contentTop=${after.contentTop}`)

  // 3) 左侧滚动：右侧不动
  await evaluate(`document.querySelector('.page-list').scrollTop = 200; true`)
  await sleep(300)
  const afterLeft = await evaluate(`(() => {
    const t = document.querySelector('.tiptap');
    const l = document.querySelector('.page-list');
    return { tTop: t.scrollTop, lTop: l.scrollTop };
  })()`)
  check('左侧列表可独立滚动', afterLeft.lTop > 0, `scrollTop=${afterLeft.lTop}`)
  check('左侧滚动时右侧正文不动', afterLeft.tTop === after.tTop,
        `${after.tTop} → ${afterLeft.tTop}`)

  // 4) 切页签：左侧换列表、右侧换页面
  const switched = await evaluate(`(() => {
    const tabs = [...document.querySelectorAll('.tabs .tab:not(.tab-more)')];
    const target = tabs[1];
    if (!target) return null;
    const name = target.textContent.trim();
    target.click();
    return name;
  })()`)
  await sleep(600)
  const afterSwitch = await evaluate(`(() => {
    const head = document.querySelector('.pages-head .muted');
    const rows = [...document.querySelectorAll('.page-list .note-row')].map(r => r.textContent.trim());
    return { head: head ? head.textContent.trim() : '', rows };
  })()`)
  check('切页签后左侧列表换成该分区',
        !!switched && afterSwitch.head.startsWith(switched) && afterSwitch.rows.length > 0,
        `${switched} → ${afterSwitch.head} / ${afterSwitch.rows.length} 页`)

  // 5) 「更多」下拉能列出被收起的
  await evaluate(`document.querySelector('.tab-more').click(); true`)
  await sleep(400)
  const moreItems = await evaluate(
    `document.querySelectorAll('.n-dropdown-option-body__label').length`)
  check('「更多」下拉列出被收起的分区', moreItems > 0, `items=${moreItems}`)
  // 关下拉必须用 ESC：headless 里点 body 不会让 naive-ui 收起
  for (const type of ['keyDown', 'keyUp']) {
    await cdp('Input.dispatchKeyEvent',
              { type, key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  }
  await sleep(400)

  // 6) 重新打开页面，截图要能看到编辑器内容
  await evaluate(`(() => {
    const row = document.querySelector('.page-list .note-row');
    if (row) row.click();
    return true;
  })()`)
  await sleep(900)

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
