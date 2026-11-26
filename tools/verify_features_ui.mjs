#!/usr/bin/env node
/**
 * 功能 1/2/3 的界面回归（2026-11-25，CDP，零第三方依赖）。
 *
 * 覆盖
 * ----
 * * 标签：运维表出现标签 chip；搜索页出现标签栏，点标签只剩该标签的条目；
 * * 保存视图：点「存为视图」→ 命名保存 → 下拉出现该视图，选中后筛选生效；
 * * 重复文件：索引页点「检查重复」→ 出现可释放空间与重复组。
 *
 * 截图：docs/screenshots/26-features-incidents.png / 27-features-duplicates.png / 28-features-search.png
 * 用法：node tools/verify_features_ui.mjs（需先 npm run build）
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
const TOKEN = 'features-ui-20261125'

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
  const size = existsSync(join(SHOTS, name)) ? 1 : 0
  check(`截图 ${name}`, size === 1)
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')
  mkdirSync(SHOTS, { recursive: true })

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_fui_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_fuichrome_'))
  const indexDir = join(dataDir, 'idx')
  mkdirSync(indexDir, { recursive: true })
  const port = await freePort()
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

  const first = await api('/api/biz/incidents', 'POST', { data: {
    occurred_at: '2026-11-20 09:00', category: 'BUG', system: 'SAP-PP',
    proposer: '张工', company: '信息中心', symptom: 'MRP 需求翻倍', status: 'open',
  } })
  await api('/api/biz/incidents', 'POST', { data: {
    occurred_at: '2026-11-05 14:10', category: '优化', system: 'SAP-MM',
    proposer: '李工', company: '业务一部', symptom: '库存报表口径', status: 'closed',
  } })
  await api(`/api/entries/${first.entry_id}/tags`, 'PUT', { tags: ['MRP', '根因分析'] })

  const q = v => "'" + String(v).replace(/'/g, "''") + "'"
  const now = Math.floor(Date.now() / 1000)
  const seed = `
INSERT INTO roots(path, kind) VALUES(${q(indexDir)}, 'local');
INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime, sha256, extract_status, indexed_at)
  SELECT id, ${q('SAP\\MRP\\需求对比_202610.xlsx')}, ${q(join(indexDir, 'a.xlsx'))}, 'xlsx', 12400000, ${now - 60}, 'dup-hash', 'ok', datetime('now','localtime') FROM roots LIMIT 1;
INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime, sha256, extract_status, indexed_at)
  SELECT id, ${q('归档\\需求对比_202610.xlsx')}, ${q(join(indexDir, 'b.xlsx'))}, 'xlsx', 12400000, ${now - 120}, 'dup-hash', 'ok', datetime('now','localtime') FROM roots LIMIT 1;
INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime, sha256, extract_status, indexed_at)
  SELECT id, ${q('SAP\\MRP\\参数表.xlsx')}, ${q(join(indexDir, 'c.xlsx'))}, 'xlsx', 860000, ${now - 300}, 'uniq-hash', 'ok', datetime('now','localtime') FROM roots LIMIT 1;
`
  const sqlPath = join(dataDir, 'seed.sql')
  writeFileSync(sqlPath, seed, 'utf8')
  const seeded = spawnSync('py', ['-3', '-c',
    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.executescript(open(sys.argv[2], encoding='utf-8').read()); c.commit(); c.close()",
    join(dataDir, 'onlyone.db'), sqlPath], { encoding: 'utf8' })
  if (seeded.status !== 0) throw new Error('造数据失败：' + (seeded.stderr || seeded.stdout))

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

  // ---------------------------------------------------------- 标签 + 视图
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=incidents` })
  await waitFor('运维记录就绪', async () => await evaluate(
    `!!document.querySelector('.n-data-table-tbody') && document.body.innerText.includes('运维记录')`))
  await sleep(700)

  const pageText = await evaluate(`document.body.innerText`)
  check('列表出现标签 chip', pageText.includes('MRP') && pageText.includes('根因分析'))
  check('筛选栏有「存为视图」', pageText.includes('存为视图'))

  // 存为视图：先选状态，再命名保存
  await evaluate(`(() => {
    const sel = [...document.querySelectorAll('.filter-bar .n-base-selection')]
      .find(s => s.textContent.includes('状态'));
    sel && sel.click();
  })()`)
  await sleep(400)
  await evaluate(`(() => {
    const hit = [...document.querySelectorAll('.n-base-select-option')]
      .find(o => o.textContent.trim() === '未关闭');
    hit && hit.click();
  })()`)
  await sleep(400)

  await evaluate(`(() => {
    const btn = [...document.querySelectorAll('.filter-bar button')]
      .find(b => b.textContent.trim() === '存为视图');
    btn && btn.click();
  })()`)
  await sleep(500)
  const modalOpen = await evaluate(`!!document.querySelector('.n-modal')`)
  await evaluate(`(() => {
    const input = document.querySelector('.n-modal input');
    if (!input) return 'no-input';
    input.focus();
    input.value = '我的 BUG';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
  })()`)
  await sleep(300)
  await evaluate(`(() => {
    const btn = [...document.querySelectorAll('.n-modal button')]
      .find(b => b.textContent.trim() === '保存');
    btn && btn.click();
  })()`)
  await sleep(900)

  const afterSave = await evaluate(`(() => {
    const sels = [...document.querySelectorAll('.filter-bar .n-base-selection')];
    return JSON.stringify({
      modal: !!document.querySelector('.n-modal'),
      hasView: sels.some(s => s.textContent.includes('我的 BUG')),
      count: Number(document.querySelector('.count-chip')?.textContent.trim() || '0'),
    });
  })()`)
  const saved = JSON.parse(afterSave)
  check('存为视图弹窗打开并保存', modalOpen && !saved.modal, JSON.stringify(saved))
  check('视图下拉出现「我的 BUG」', saved.hasView)
  check('视图筛选生效（未关闭 1 条）', saved.count === 1, `count=${saved.count}`)
  await shot('26-features-incidents.png')

  // ---------------------------------------------------------- 重复文件
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=roots` })
  await waitFor('索引页就绪', async () => await evaluate(
    `document.body.innerText.includes('重复文件')`))
  await sleep(600)
  await evaluate(`(() => {
    const btn = [...document.querySelectorAll('button')]
      .find(b => b.textContent.trim() === '检查重复');
    btn && btn.click();
  })()`)
  await sleep(1200)
  const dupText = await evaluate(`document.body.innerText`)
  check('重复文件出现指标', dupText.includes('可释放') && dupText.includes('冗余文件'))
  const pathHits = dupText.split('需求对比_202610.xlsx').length - 1
  check('重复组列出两条路径', pathHits >= 2, `hits=${pathHits}`)
  // 重复文件卡片在页面下方；主内容区是 .content 自己的滚动容器，window.scrollTo 无效
  await evaluate(`(() => {
    const card = [...document.querySelectorAll('.card')]
      .find(c => c.textContent.includes('重复文件'));
    if (card) card.scrollIntoView({ block: 'end' });
    return !!card;
  })()`)
  await sleep(600)
  await shot('27-features-duplicates.png')

  // ---------------------------------------------------------- 搜索页标签栏
  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=search` })
  await waitFor('搜索页就绪', async () => await evaluate(
    `!!document.querySelector('.tag-bar') || document.body.innerText.includes('输入关键词开始搜索')`))
  await sleep(600)
  const tagBar = await evaluate(`(() => {
    const bar = document.querySelector('.tag-bar');
    return bar ? bar.innerText.replace(/\\s+/g, ' ').trim() : '';
  })()`)
  check('搜索页出现标签栏', tagBar.includes('MRP'), tagBar)

  await evaluate(`(() => {
    const pill = [...document.querySelectorAll('.tag-pill')]
      .find(p => p.textContent.includes('MRP'));
    pill && pill.click();
  })()`)
  await sleep(900)
  const searchText = await evaluate(`document.body.innerText`)
  check('点标签后只剩该标签条目', searchText.includes('MRP 需求翻倍')
        && !searchText.includes('库存报表口径'))
  await shot('28-features-search.png')

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
