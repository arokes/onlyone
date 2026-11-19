#!/usr/bin/env node
/**
 * 在真实 pywebview(WebView2) 窗口里点「启动 SAP」，验证密码复制是否成功。
 * 前置：run.py 以 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223 启动。
 */
const DBG = 'http://127.0.0.1:9223'
const sleep = ms => new Promise(r => setTimeout(r, ms))

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.waiters = new Map() }
  static async open() {
    const list = await (await fetch(DBG + '/json/list')).json()
    const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl)
    const ws = new WebSocket(page.webSocketDebuggerUrl)
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
    const cdp = new CDP(ws)
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data)
      if (m.id) { const w = cdp.waiters.get(m.id); if (w) { cdp.waiters.delete(m.id); w(m) } }
    }
    return cdp
  }
  send(method, params = {}) {
    const id = ++this.id
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise(res => this.waiters.set(id, res))
  }
  async eval(expr, userGesture = true) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr, awaitPromise: true, returnByValue: true, userGesture,
    })
    const ex = r.result?.exceptionDetails
    if (ex) return '异常: ' + (ex.exception?.description || ex.text)
    return r.result?.result?.value
  }
}

async function main() {
  const cdp = await CDP.open()
  await cdp.send('Runtime.enable')
  console.log('窗口:', await cdp.eval('document.title + " | focus=" + document.hasFocus()', false))

  console.log('注入探针:', await cdp.eval(`(() => {
    window.__clip = []
    window.__fetches = []
    const orig = navigator.clipboard.writeText.bind(navigator.clipboard)
    navigator.clipboard.writeText = async (t) => {
      window.__clip.push({ phase: 'call', at: Math.round(performance.now()), len: String(t).length })
      try {
        await orig(t)
        window.__clip.push({ phase: 'ok', at: Math.round(performance.now()) })
      } catch (e) {
        window.__clip.push({ phase: 'err', at: Math.round(performance.now()),
                             err: e.name + ': ' + e.message })
        throw e
      }
    }
    const of = window.fetch
    window.fetch = (...a) => {
      window.__fetches.push({ url: String(a[0]), at: Math.round(performance.now()) })
      return of(...a)
    }
    return 'OK secure=' + window.isSecureContext
  })()`))

  console.log('选中绑定行:', await cdp.eval(`(async () => {
    const menu = [...document.querySelectorAll('*')].find(
      e => e.children.length === 0 && /凭据/.test(e.textContent || ''))
    if (menu) menu.click()
    await new Promise(r => setTimeout(r, 1500))
    const tk = new URLSearchParams(location.search).get('token') || ''
    const list = await (await fetch('/api/credentials', { headers: { 'X-OnlyOne-Token': tk } })).json()
    const b = list.find(x => x.sap_conn)
    if (!b) return '没有绑定 SAP 的凭据'
    const row = [...document.querySelectorAll('.n-data-table-tr')]
      .find(r => (r.textContent || '').includes(b.title))
    if (!row) return '表格里找不到 ' + b.title
    row.click()
    await new Promise(r => setTimeout(r, 400))
    window.__target = b.title + ' / ' + b.sap_conn
    return window.__target
  })()`))

  console.log('点击启动:', await cdp.eval(`(() => {
    const btn = [...document.querySelectorAll('button, .n-button')].find(
      e => /启动 SAP/.test(e.textContent || ''))
    if (!btn) return '没找到按钮'
    btn.click()
    return '已点击，focus=' + document.hasFocus()
  })()`))

  let prev = ''
  for (let i = 0; i < 30; i++) {
    await sleep(400)
    const snap = await cdp.eval(`JSON.stringify({
      clip: window.__clip, focus: document.hasFocus(),
      msg: [...document.querySelectorAll('.n-message__content')].map(e => e.textContent),
    })`, false)
    if (snap && snap !== prev) {
      const o = JSON.parse(snap)
      console.log(`  t=${((i + 1) * 0.4).toFixed(1)}s focus=${o.focus} clip=${JSON.stringify(o.clip)} msg=${JSON.stringify(o.msg)}`)
      prev = snap
    }
  }
  console.log('\n时序:', await cdp.eval(`JSON.stringify({
    clip: window.__clip,
    openSap: (window.__fetches || []).filter(f => /open-sap/.test(f.url)),
  })`, false))
}

main().catch(e => { console.error('FAIL', e); process.exit(1) })
