#!/usr/bin/env node
/**
 * 概览 → 全局搜索 → 预览 的跳转回归（2026-11-25，CDP，零第三方依赖）。
 *
 * 修的问题：从概览点「最近改动」只打开预览、不带查询词，关掉预览后搜索页
 * 是空搜索框 + 空态（文案还误导成「去添加索引目录」）。
 *
 * 断言
 * ----
 * * 点第一行后落在搜索页，搜索框里是该文件名（去扩展名）；
 * * 结果区有命中，预览抽屉同时打开；
 * * 关闭抽屉后仍是结果列表，不出现「还没有可搜索的内容」空态；
 * * 截图 docs/screenshots/25-overview-jump.png。
 *
 * 用法：node tools/verify_overview_jump.mjs
 * 前置：npm run build 已生成 frontend/dist。脚本自起后端 + 独立临时库，不碰 data/onlyone.db。
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const SHOT = join(ROOT, 'docs', 'screenshots', '25-overview-jump.png')
const TOKEN = 'overview-jump-20261125'

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

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_jump_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_jumpchrome_'))
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

  // 直接写库：走 API 注册目录会触发 watcher 校准，把这里伪造的文件行当成"已消失"清掉。
  // SQL 交给 Python 执行：Node 的 node:sqlite 在 22.x 还需要实验开关，且中文走命令行不安全。
  const TARGET = 'JS-PP-09 产销协调流程操作手册'
  const now = Math.floor(Date.now() / 1000)
  const q = v => "'" + String(v).replace(/'/g, "''") + "'"
  const seedSql = `
INSERT INTO roots(path, kind) VALUES(${q(indexDir)}, 'local');
INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime, sha256, extract_status, indexed_at)
  SELECT id, ${q('SAP\\' + TARGET + '.doc')}, ${q(join(indexDir, 'a.doc'))}, 'doc', 40960, ${now - 600},
         '', 'ok', datetime('now','localtime') FROM roots LIMIT 1;
INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime, sha256, extract_status, indexed_at)
  SELECT id, ${q('制度\\BOM和工艺路线接口报错汇总.docx')}, ${q(join(indexDir, 'b.docx'))}, 'docx', 98304, ${now - 7200},
         '', 'ok', datetime('now','localtime') FROM roots LIMIT 1;
INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime, sha256, extract_status, indexed_at)
  SELECT id, ${q('旧\\会议纪要.txt')}, ${q(join(indexDir, 'c.txt'))}, 'txt', 2048, ${now - 86400},
         '', 'ok', datetime('now','localtime') FROM roots LIMIT 1;
INSERT INTO entries(type, ref_id, title, summary, occurred_at, tags)
  SELECT 'file', id, ${q(TARGET)}, ${q(TARGET)}, datetime('now','localtime'), '文件'
  FROM files ORDER BY mtime DESC LIMIT 1;
INSERT INTO fts_entries(entry_id, title, body, tags)
  SELECT id, title, ${q(TARGET + '\n产销协调流程与订单下达要求')}, '文件'
  FROM entries WHERE type='file' ORDER BY id DESC LIMIT 1;
`
  const sqlPath = join(dataDir, 'seed.sql')
  writeFileSync(sqlPath, seedSql, 'utf8')
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

  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}` })
  await waitFor('概览就绪', async () => await evaluate(
    `!!document.querySelector('.home-row') && document.body.innerText.includes('最近改动')`))
  await sleep(400)

  // 取「最近改动」卡片第一行的标题并点击
  const clicked = await evaluate(`(() => {
    const card = [...document.querySelectorAll('.card')]
      .find(c => c.textContent.includes('最近改动'));
    const row = card?.querySelector('.home-row');
    if (!row) return 'no-row';
    const title = row.querySelector('.cell-main')?.textContent.trim() || '';
    row.click();
    return title;
  })()`)
  check('点到最近改动第一行', clicked !== 'no-row' && !!clicked, String(clicked))

  await sleep(2600)  // 异步组件挂载 + 检索 + 预览请求

  const view = JSON.parse(await evaluate(`(() => {
    const input = document.querySelector('.onlyone-search input');
    const bar = document.querySelector('.result-bar');
    const groups = document.querySelectorAll('.group-title');
    const drawer = document.querySelector('.n-drawer');
    return JSON.stringify({
      keyword: input ? input.value : '',
      hasBar: !!bar,
      barText: bar ? bar.textContent.replace(/\\s+/g, ' ').trim() : '',
      groups: groups.length,
      drawerOpen: !!drawer && drawer.getBoundingClientRect().width > 0,
      body: document.body.innerText,
    });
  })()`))

  const expected = clicked.replace(/\.[A-Za-z0-9]{1,5}$/, '')
  check('跳到搜索页且搜索框已填文件名', view.keyword === expected, `"${view.keyword}"`)
  check('搜索已执行并出现结果分组', view.hasBar && view.groups > 0,
        `groups=${view.groups} ${view.barText.slice(0, 40)}`)
  check('目标文件出现在结果里', view.body.includes(expected))
  check('预览抽屉同时打开', view.drawerOpen)

  // 关闭抽屉
  const closed = await evaluate(`(() => {
    const btn = document.querySelector('.n-drawer .n-base-close');
    if (!btn) return 'no-close';
    btn.click();
    return 'ok';
  })()`)
  await sleep(700)
  const after = JSON.parse(await evaluate(`(() => {
    const drawer = document.querySelector('.n-drawer');
    return JSON.stringify({
      drawerOpen: !!drawer && drawer.getBoundingClientRect().width > 0,
      groups: document.querySelectorAll('.group-title').length,
      empty: document.body.innerText.includes('还没有可搜索的内容'),
      hasTarget: document.body.innerText.includes(${JSON.stringify(expected)}),
    });
  })()`))
  check('关闭抽屉', closed === 'ok', closed)
  check('关闭后仍在结果列表', !after.drawerOpen && after.groups > 0 && after.hasTarget,
        `groups=${after.groups}`)
  check('关闭后不再出现误导性空态', !after.empty)

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
