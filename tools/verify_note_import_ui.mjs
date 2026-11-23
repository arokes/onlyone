#!/usr/bin/env node
/**
 * 记事本「导入 OneNote 分区」界面回归（CDP，零第三方依赖）。
 *
 * 覆盖：顶部「更多」菜单含导入项 → 通过隐藏 input 选入 .mht → 后端建分区与页面 →
 * 结果弹窗显示统计 → 截图。自包含：不依赖 D:\1118 里的真实导出文件，
 * 内置一份最小 MHT 样本（1 页 + 1 张 1x1 PNG）。
 *
 * 用法：node tools/verify_note_import_ui.mjs
 * 可选：--mht "D:\1118\MM.MHT"  改用真实导出文件（页数更多，验证更充分）
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
const SHOT = join(ROOT, 'docs', 'screenshots', '14-notes-import-ui.png')
const TOKEN = 'note-import-ui-20261118'
const BOOK = '导入验证'

const PNG_1PX = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

/** 最小可用的 OneNote 风格 .mht：一个页面 + 一张图，HTML 用 base64 传输避免编码坑。 */
function buildSampleMht(title = '界面导入验证页', bodyText = '正文第一行 ABC') {
  const html = `<html><body>
<div style="direction:ltr;border-width:100%">
<p style='margin:0in;font-family:"Microsoft YaHei Light";font-size:20.0pt'>${title}</p>
<p style="margin:0in;font-size:10.0pt;color:#767676">2026年11月18日</p>
<p style="margin:0in;font-family:宋体;font-size:12.0pt">${bodyText}</p>
<p style="margin:0in;font-family:宋体;font-size:12.0pt">正文第二行 DEF</p>
<table border="1" style="border-collapse:collapse">
<tr><td><p style="margin:0in;font-size:12.0pt">列一</p></td>
    <td><p style="margin:0in;font-size:12.0pt">列二</p></td></tr>
<tr><td><p style="margin:0in;font-size:12.0pt">值一</p></td>
    <td><p style="margin:0in;font-size:12.0pt">值二</p></td></tr>
</table>
<p style="margin:0in"><img src="sample.files/image001.png" width="40" height="40" alt="图片"></p>
</div>
</body></html>`
  const htmlB64 = Buffer.from(html, 'utf-8').toString('base64')
  return [
    'MIME-Version: 1.0',
    'Content-Type: multipart/related; boundary="BOUNDARY"; type="text/html"',
    '',
    '--BOUNDARY',
    'Content-Type: text/html; charset=utf-8',
    'Content-Transfer-Encoding: base64',
    'Content-Location: file:///C:/sample/sample.htm',
    '',
    htmlB64,
    '',
    '--BOUNDARY',
    'Content-Type: image/png',
    'Content-Transfer-Encoding: base64',
    'Content-Location: file:///C:/sample/sample.files/image001.png',
    '',
    PNG_1PX,
    '',
    '--BOUNDARY--',
    '',
  ].join('\r\n')
}

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

let backend, chrome, ws, dataDir, profileDir, mhtPath
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

  const argMht = process.argv.find(a => a.startsWith('--mht='))?.slice(6)
    || (process.argv.includes('--mht') ? process.argv[process.argv.indexOf('--mht') + 1] : '')
  if (argMht) {
    if (!existsSync(argMht)) throw new Error('指定的 .mht 不存在：' + argMht)
    mhtPath = argMht
  } else {
    const dir = mkdtempSync(join(tmpdir(), 'mht_sample_'))
    mhtPath = join(dir, 'Sample.MHT')
    writeFileSync(mhtPath, buildSampleMht(), 'utf-8')
  }

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_imp_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_impchrome_'))
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

  await api('/api/notebooks', 'POST', { name: BOOK })

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
  await cdp('DOM.enable')
  await cdp('Emulation.setDeviceMetricsOverride',
            { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=notes` })
  await waitFor('页面加载', async () => await evaluate(`document.readyState === 'complete'`))
  await evaluate(`localStorage.setItem('onlyone_setup_skipped','1'); true`)
  await cdp('Page.reload')
  await waitFor('记事本就绪', async () =>
    await evaluate(`!!document.querySelector('.tiptap-body, .notes-layout') && document.readyState === 'complete'`))

  // 1) 顶部「更多」按钮存在且可用，菜单里能点到导入项（2026-11-20：导入/导出合并进「更多」）
  const btnState = await evaluate(`(() => {
    const btn = [...document.querySelectorAll('.notes-topbar button')]
      .find(b => b.textContent.trim().startsWith('更多'));
    if (!btn) return null;
    btn.click();
    return { text: btn.textContent.trim(), disabled: btn.disabled };
  })()`)
  await new Promise(r => setTimeout(r, 400))
  const importItem = await evaluate(`(() => {
    const item = [...document.querySelectorAll('.n-dropdown-option')]
      .find(o => o.textContent.includes('导入 OneNote'));
    return item ? item.textContent.trim() : null;
  })()`)
  check('顶部「更多」菜单含导入项', !!btnState && !btnState.disabled && !!importItem,
        JSON.stringify({ btnState, importItem }))
  await evaluate(`document.body.click(); true`)

  // 2) 通过隐藏 input 选入 MHT（等价于用户点选文件）
  const { root } = await cdp('DOM.getDocument', { depth: 2 })
  const { nodeId } = await cdp('DOM.querySelector', {
    nodeId: root.nodeId,
    selector: 'input[type=file]:not([webkitdirectory])',
  })
  check('隐藏的 .mht 文件输入存在', !!nodeId && nodeId > 0, `nodeId=${nodeId}`)
  await cdp('DOM.setFileInputFiles', { nodeId, files: [mhtPath] })

  // 3) 等后端真的建出分区
  const sectionName = mhtPath.split(/[\\/]/).pop().replace(/\.(mht|mhtml)$/i, '')
  const imported = await waitFor('导入完成', async () => {
    const books = await api('/api/notebooks')
    const book = books.find(b => b.name === BOOK)
    return (book?.sections || []).some(s => s.name === sectionName)
  }, 60000).then(() => true).catch(() => false)
  check('导入后新建了分区', imported, `section=${sectionName}`)

  const detail = await api('/api/notebooks')
  const book = detail.find(b => b.name === BOOK)
  const section = (book?.sections || []).find(s => s.name === sectionName)
  check('分区内有页面', !!section && section.notes.length >= 1,
        `pages=${section?.notes.length ?? 0}`)

  // 4) 内容落库：整节范围内检查块结构与图片（第一页未必有表格）
  if (section?.notes?.length) {
    const kinds = new Set()
    let imageCount = 0
    let firstImageUrl = ''
    for (const note of section.notes.slice(0, 40)) {
      const detailNote = await api(`/api/notes/${note.id}`)
      const cj = detailNote.content_json || ''
      try {
        for (const block of JSON.parse(cj || '{"content":[]}').content || []) {
          kinds.add(block.type)
        }
      } catch { /* 跳过坏 JSON */ }
      imageCount += (cj.split('/api/attachments/').length - 1)
      if (!firstImageUrl) {
        const m = /\/api\/attachments\/[0-9]+\/[0-9a-f]{32}\.[a-z]+/.exec(cj)
        if (m) firstImageUrl = m[0]
      }
    }
    check('页面标题来自 OneNote 标题', section.notes[0].title.length > 0,
          `title=${section.notes[0].title}`)
    check('正文块结构正确（段落 + 表格）',
          kinds.has('paragraph') && kinds.has('table'), JSON.stringify([...kinds]))
    if (imageCount) {
      const resp = await fetch(base + firstImageUrl, { headers: { 'X-OnlyOne-Token': TOKEN } })
      const buf = await resp.arrayBuffer()
      const signature = Buffer.from(buf.slice(0, 4))
      const isImage = signature[0] === 0x89 || (signature[0] === 0xff && signature[1] === 0xd8)
      check('图片已落盘且可访问', resp.ok && isImage, `${imageCount} 张 / 首图 ${buf.byteLength}B`)
    } else {
      console.log('SKIP 图片（该文件本身没有内嵌图）')
    }
  }

  // 5) 文件夹选择：一次选中整个导出目录
  const dirPath = mkdtempSync(join(tmpdir(), 'mht_dir_'))
  writeFileSync(join(dirPath, 'Sample2.MHT'),
                buildSampleMht('目录导入验证页', '目录里的第二页'), 'utf-8')
  const docAfter = await cdp('DOM.getDocument', { depth: 2 })
  const dirNode = await cdp('DOM.querySelector', {
    nodeId: docAfter.root.nodeId,
    selector: 'input[type=file][webkitdirectory]',
  })
  check('文件夹输入存在', !!dirNode.nodeId && dirNode.nodeId > 0, `nodeId=${dirNode.nodeId}`)
  if (dirNode.nodeId) {
    await cdp('DOM.setFileInputFiles', { nodeId: dirNode.nodeId, files: [dirPath] })
    const dirOk = await waitFor('目录导入完成', async () => {
      const books = await api('/api/notebooks')
      const book = books.find(b => b.name === BOOK)
      return (book?.sections || []).some(s => s.name === 'Sample2')
    }, 60000).then(() => true).catch(() => false)
    check('文件夹导入生成新分区', dirOk, 'Sample2')
  }

  // 6) 结果弹窗
  await sleep(500)
  const modalText = await evaluate(`(() => {
    const m = document.querySelector('.n-modal');
    return m ? m.innerText.replace(/\\s+/g, ' ').slice(0, 160) : '';
  })()`)
  check('导入后弹出结果统计', /导入结果/.test(modalText), modalText.slice(0, 80))

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
