#!/usr/bin/env node
/**
 * 运维记录筛选回归（CDP，零第三方依赖）。
 *
 * 覆盖：全文搜索（跨富文本字段）· 公司下拉（从现有记录去重）· 状态 ·
 *       项目 · 发生时间区间 · 清除筛选 · 无结果空状态。
 *
 * 用法：node tools/verify_incident_filters.mjs
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
const SHOT = join(ROOT, 'docs', 'screenshots', '23-incident-filters.png')
const TOKEN = 'incident-filter-20261124'

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

/** 计数读顶部 chip（= 筛选后行数），比数 tr 稳（空表也有占位行） */
async function countChip() {
  return Number(await evaluate(
    `document.querySelector('.count-chip')?.textContent.trim() || ''`))
}
async function rowTitles() {
  return JSON.parse(await evaluate(`
    JSON.stringify([...document.querySelectorAll('.n-data-table-tbody .n-data-table-tr')]
      .map(tr => tr.innerText.replace(/\\s+/g, ' ').trim()))`))
}
async function setKeyword(text) {
  await evaluate(`(() => {
    const input = document.querySelector('.filter-bar input[placeholder^="全文搜索"]');
    if (!input) return 'no-input';
    input.focus();
    input.value = ${JSON.stringify(text)};
    input.dispatchEvent(new Event('input', { bubbles: true }));
    return 'ok';
  })()`)
  await sleep(350)
}
async function openSelect(label) {
  return evaluate(`(() => {
    const sels = [...document.querySelectorAll('.filter-bar .n-base-selection')];
    const hit = sels.find(s => s.textContent.includes(${JSON.stringify(label)}));
    if (!hit) return 'no-select';
    hit.click();
    return 'ok';
  })()`)
}
async function pickOption(text) {
  return evaluate(`(() => {
    const items = [...document.querySelectorAll('.n-base-select-option')];
    const hit = items.find(o => o.textContent.trim() === ${JSON.stringify(text)});
    if (!hit) return 'no-option';
    hit.click();
    return 'ok';
  })()`)
}
async function goto(label) {
  await evaluate(`(() => {
    const btn = [...document.querySelectorAll('.nav-item')]
      .find(b => b.textContent.trim() === '${label}');
    if (!btn) return 'no-nav';
    btn.click();
    return 'ok';
  })()`)
  await sleep(700)
}
async function clearFilters() {
  await evaluate(`(() => {
    const btn = [...document.querySelectorAll('.filter-bar button')]
      .find(b => b.textContent.trim() === '清除筛选');
    if (btn && !btn.disabled) { btn.click(); return 'ok'; }
    return 'no-op';
  })()`)
  await sleep(350)
}

async function main() {
  if (!existsSync(CHROME)) throw new Error('未找到 Chrome：' + CHROME)
  if (!existsSync(join(ROOT, 'frontend', 'dist', 'index.html')))
    throw new Error('前端未构建，请先 npm run build')

  dataDir = mkdtempSync(join(tmpdir(), 'onlyone_filters_'))
  profileDir = mkdtempSync(join(tmpdir(), 'onlyone_filterschrome_'))
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

  const idxDir = join(dataDir, 'idx')
  mkdirSync(idxDir, { recursive: true })
  await api('/api/roots', 'POST', { path: idxDir, kind: 'local' })

  const project = await api('/api/biz/projects', 'POST', { data: { name: '筛选验证项目' } })
  const doc = text => JSON.stringify({
    type: 'doc',
    content: [{ type: 'paragraph', content: [{ type: 'text', text }] }],
  })
  const seed = [
    { occurred_at: '2026-11-20 09:00', category: 'BUG', system: 'SAP-PP', proposer: '张工',
      company: '信息中心', symptom: 'MRP 需求翻倍', status: 'open',
      project_id: project.id },
    { occurred_at: '2026-11-05 14:10', category: '优化', system: 'SAP-MM', proposer: '李工',
      company: '业务一部', symptom: '库存报表口径不一致', status: 'closed' },
    { occurred_at: '2026-11-22 08:30', category: '业务方案', system: 'SAP-FI', proposer: '王工',
      company: '信息中心', symptom: '凭证过账失败', status: 'closed',
      project_id: project.id },
    { occurred_at: '2026-10-30 21:00', category: '流程效率', system: '网关', proposer: '赵工',
      company: '云平台部', symptom: '网关超时', status: 'open' },
  ]
  for (const item of seed) {
    await api('/api/biz/incidents', 'POST', { data: {
      ...item,
      symptom: doc(item.symptom),
      root_cause: doc('原因'),
      action: doc('处置动作'),
      duration_min: 30,
    } })
  }

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

  await cdp('Page.navigate', { url: `${base}/?token=${TOKEN}&tab=incidents` })
  await waitFor('运维记录就绪', async () => await evaluate(
    `!!document.querySelector('.filter-bar') && document.querySelector('.count-chip') !== null`))
  await sleep(700)

  check('初始列出全部 4 条', await countChip() === 4, `count=${await countChip()}`)

  // 0) 超期标黄：未关闭且挂起 > 7 天（10-30 那条必超期；已关闭的不标）
  const overdueRows = JSON.parse(await evaluate(`
    JSON.stringify([...document.querySelectorAll('.n-data-table-tr.row-overdue')]
      .map(tr => tr.innerText.replace(/\\s+/g, ' ').trim()))`))
  const overdueText = overdueRows.join('|')
  check('超期未关闭行标黄，已关闭行不标',
        overdueText.includes('网关超时') && !overdueText.includes('库存报表口径不一致'),
        overdueText.slice(0, 100))

  // 1) 全文搜索：命中富文本正文
  await setKeyword('MRP')
  check('关键字命中富文本正文', await countChip() === 1, `count=${await countChip()}`)

  // 2) 全文搜索：命中提出人 / 公司这类普通字段
  await setKeyword('信息中心')
  check('关键字命中公司字段', await countChip() === 2, `count=${await countChip()}`)

  // 3) 空格分词 AND
  await setKeyword('MRP 翻倍')
  check('多词 AND 同时命中', await countChip() === 1, `count=${await countChip()}`)
  await setKeyword('MRP 不存在')
  // naive 把 empty 插槽渲染在 .n-data-table-empty（不在 tbody 里），所以查整页文本
  const emptyText = await evaluate(`document.body.innerText || ''`)
  check('无结果显示「没有匹配的记录」',
        (await countChip()) === 0 && emptyText.includes('没有匹配的记录'),
        `count=${await countChip()} hasEmptyText=${emptyText.includes('没有匹配的记录')}`)
  await clearFilters()
  check('清除筛选后恢复 4 条', await countChip() === 4, `count=${await countChip()}`)

  // 4) 公司下拉：候选值来自现有记录，选后精确过滤
  await openSelect('公司')
  await sleep(400)
  const companyOptions = JSON.parse(await evaluate(`
    JSON.stringify([...document.querySelectorAll('.n-base-select-option')]
      .map(o => o.textContent.trim()))`))
  check('公司候选值来自现有记录',
        companyOptions.includes('信息中心') && companyOptions.includes('云平台部')
        && companyOptions.length === 3, companyOptions.join('/'))
  check('选中公司后过滤', (await pickOption('信息中心')) === 'ok'
        && (await sleep(400), await countChip()) === 2, `count=${await countChip()}`)
  await clearFilters()

  // 4.5) 类型（固定五档，不走「从现有记录去重」）：选后精确过滤
  await openSelect('类型')
  await sleep(400)
  const typeOptions = JSON.parse(await evaluate(`
    JSON.stringify([...document.querySelectorAll('.n-base-select-option')]
      .map(o => o.textContent.trim()))`))
  const EXPECT_TYPES = ['BUG', '优化', '新需求', '业务方案', '流程效率']
  // 公司下拉的菜单是 teleport 到 body 的，关闭后仍留在 DOM 里，
  // 所以这里只校验「五档齐全且无重复」，不要求选项总数恰好为 5。
  const hitTypes = typeOptions.filter(t => EXPECT_TYPES.includes(t))
  check('类型候选为固定五档',
        hitTypes.length === EXPECT_TYPES.length
        && EXPECT_TYPES.every(t => hitTypes.includes(t)),
        typeOptions.join('/'))
  check('选中类型「优化」后过滤', (await pickOption('优化')) === 'ok'
        && (await sleep(400), await countChip()) === 1, `count=${await countChip()}`)
  check('类型列显示中文原值', (await rowTitles()).join('|').includes('优化'))
  await clearFilters()

  // 5) 状态
  await openSelect('状态')
  await sleep(400)
  await pickOption('已关闭')
  await sleep(400)
  check('状态筛选 已关闭 = 2 条', await countChip() === 2, `count=${await countChip()}`)
  await clearFilters()

  // 6) 项目
  await openSelect('项目')
  await sleep(400)
  await pickOption('筛选验证项目')
  await sleep(400)
  check('项目筛选命中 2 条', await countChip() === 2, `count=${await countChip()}`)
  await clearFilters()

  // 7) 发生时间区间：两个单日期控件，各点一次（起 11-01，止 11-21，闭区间）
  const pickDay = async (index, day) => {
    await evaluate(`(() => {
      const inputs = [...document.querySelectorAll('.filter-bar .n-date-picker input')];
      const input = inputs[${index}];
      if (!input) return 'no-input';
      input.focus();
      input.click();
      return 'ok';
    })()`)
    await sleep(500)
    const r = await evaluate(`(() => {
      const panel = document.querySelector('.n-date-panel');
      if (!panel) return 'no-panel';
      const cell = [...panel.querySelectorAll('.n-date-panel-date:not(.n-date-panel-date--excluded)')]
        .find(c => c.textContent.trim() === '${day}');
      if (!cell) return 'no-cell';
      (cell.querySelector('.n-date-panel-date__trigger') || cell).click();
      return 'ok';
    })()`)
    await sleep(500)
    return r
  }
  const p1 = await pickDay(0, '1')
  const p2 = await pickDay(1, '21')
  const dateVals = await evaluate(`
    JSON.stringify([...document.querySelectorAll('.filter-bar .n-date-picker input')]
      .map(i => i.value))`)
  const titles = await rowTitles()
  check('时间区间 11-01~11-21 只留 2 条',
        p1 === 'ok' && p2 === 'ok' && (await countChip()) === 2,
        `p1=${p1} p2=${p2} ${dateVals} count=${await countChip()}`)
  check('区间结果不含 11-22 那条',
        !titles.join('|').includes('凭证过账失败'), titles.join(' | ').slice(0, 80))

  // 8) 切走再回来：筛选条件保留（关键字 + 状态）
  await clearFilters()
  await setKeyword('信息中心')
  await openSelect('状态')
  await sleep(400)
  await pickOption('已关闭')
  await sleep(400)
  const before = await countChip()
  check('组合筛选（关键字+状态）命中 1 条', before === 1, `count=${before}`)

  await goto('记事本')
  await goto('运维记录')
  await waitFor('筛选栏回来', async () => await evaluate(`!!document.querySelector('.filter-bar')`))
  await sleep(600)

  const restored = JSON.parse(await evaluate(`(() => {
    const input = document.querySelector('.filter-bar input[placeholder^="全文搜索"]');
    const sels = [...document.querySelectorAll('.filter-bar .n-base-selection')];
    return JSON.stringify({
      keyword: input ? input.value : '',
      status: sels.some(s => s.textContent.includes('已关闭')),
      count: Number(document.querySelector('.count-chip')?.textContent.trim() || '0'),
    });
  })()`))
  check('切回来关键字筛选仍保留', restored.keyword === '信息中心', `keyword=${restored.keyword}`)
  check('切回来状态下拉仍为已关闭', restored.status === true)
  check('切回来后结果仍被过滤', restored.count === 1, `count=${restored.count}`)

  // 截图留证（带筛选条件的状态）
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
