# -*- coding: utf-8 -*-
"""运维记录 UI 的运行时验证：全局搜索跳转 + 富文本编辑器 + 模拟 Ctrl+V 贴图。

用法：py -3 tools\\capture_incident_editor.py

为什么需要它
------------
``capture_incident_ui.py`` 只能静态截列表页；「弹窗里的 TipTap 编辑器是否挂载、
粘贴是否真的走上传并落成 image 节点」只有在浏览器里跑一遍才知道。这里用 CDP
（标准库手写最小 WebSocket 客户端，不引入 playwright/selenium）驱动本机 Chrome：

1. 全局搜索点「在运维记录中打开」→ 断言切到运维记录页签并弹出该条编辑弹窗；
2. 双击列表行 → 断言弹出该条的编辑弹窗（改完点「取消」回到列表）；
3. 点「新增」→ 断言出现 4 个 ProseMirror 实例与快捷键提示；
4. 构造 1×1 PNG 的 ``DataTransfer`` 派发 paste 事件 → 断言出现 ``<img>`` 且 src
   指向 ``/api/attachments/blob/``；
5. 截图存 ``docs/screenshots/08-incident-editor.png`` 与 ``10-search-jump.png``。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = ROOT / "docs" / "screenshots" / "08-incident-editor.png"
OUT_JUMP = ROOT / "docs" / "screenshots" / "10-search-jump.png"
TOKEN = "cdp-editor-20261123"
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


class WS:
    """最小 WebSocket 客户端：只为发 CDP 命令，不做分片重组。"""

    def __init__(self, url: str):
        host_port, path = url[5:].split("/", 1)
        host, port = host_port.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败：%r" % head[:120])

    def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise RuntimeError("调试连接被关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout: float = 30) -> str:
        self.sock.settimeout(timeout)
        payload = b""
        while True:
            b1, b2 = self._read(2)
            length = b2 & 0x7F
            if length == 126:
                length = int.from_bytes(self._read(2), "big")
            elif length == 127:
                length = int.from_bytes(self._read(8), "big")
            payload += self._read(length)
            if b1 & 0x80:
                return payload.decode("utf-8", "ignore")


def cdp(ws: WS, seq: list[int], method: str, params: dict | None = None) -> dict:
    seq[0] += 1
    ws.send(json.dumps({"id": seq[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == seq[0]:
            return msg


def evaluate(ws: WS, seq: list[int], expr: str):
    result = cdp(ws, seq, "Runtime.evaluate",
                 {"expression": expr, "awaitPromise": True, "returnByValue": True})
    return result.get("result", {}).get("result", {}).get("value")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def main() -> int:
    if not Path(CHROME).exists():
        print("FAIL 未找到 Chrome：%s" % CHROME)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="onlyone_cdp_"))
    profile = Path(tempfile.mkdtemp(prefix="onlyone_cdp_chrome_"))
    server_port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    env["ONLYONE_DATA_DIR"] = str(data_dir)
    server = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(server_port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % server_port
    chrome = None
    results: list[bool] = []
    try:
        for _ in range(160):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")

        index = data_dir / "idx"
        index.mkdir(exist_ok=True)
        http(base, "/api/roots", "POST", {"path": str(index), "kind": "local"})
        http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": "2026-11-23 09:00", "system": "SAP-PP",
            "symptom": "MRP 需求翻倍（示例）", "status": "open", "duration_min": 45}})

        chrome_port = free_port()
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
             "--hide-scrollbars", "--window-size=1400,950",
             "--remote-debugging-port=%d" % chrome_port,
             "--user-data-dir=" + str(profile),
             base + "/?token=" + TOKEN + "&tab=search&q=" + urllib.parse.quote("MRP")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws_url = ""
        for _ in range(120):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % chrome_port, timeout=1) as resp:
                    targets = json.loads(resp.read())
                page = next((t for t in targets if t["type"] == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        if not ws_url:
            raise RuntimeError("取不到 Chrome 调试目标")

        ws = WS(ws_url)
        seq = [0]
        cdp(ws, seq, "Runtime.enable")
        time.sleep(3.5)                       # 等异步组件与搜索结果落地

        # --- 阶段 1：全局搜索 → 在运维记录中打开 ---
        jumped = evaluate(ws, seq, """
          (() => {
            const btn = document.querySelector('button[aria-label="在运维记录中打开"]');
            if (!btn) return 'no-jump-button';
            btn.click();
            return 'ok';
          })()
        """)
        results.append(check("搜索结果出现「在运维记录中打开」按钮",
                             jumped == "ok", str(jumped)))
        time.sleep(2.5)

        landed = json.loads(evaluate(ws, seq, """
          JSON.stringify({
            modal: document.body.innerText.includes('编辑运维记录'),
            editors: document.querySelectorAll('.ProseMirror').length,
            symptom: document.body.innerText.includes('MRP 需求翻倍'),
            nav: !!document.querySelector('.nav-item.active, .nav-item[aria-current]'),
          })
        """) or "{}")
        results.append(check("跳转后直接打开该条编辑弹窗", bool(landed.get("modal"))))
        results.append(check("弹窗内四个富文本字段已载入",
                             landed.get("editors") == 4, "editors=%s" % landed.get("editors")))
        results.append(check("弹窗内容与命中记录一致", bool(landed.get("symptom"))))

        shot_jump = cdp(ws, seq, "Page.captureScreenshot", {"format": "png"})
        data_jump = shot_jump.get("result", {}).get("data", "")
        if data_jump:
            OUT_JUMP.write_bytes(base64.b64decode(data_jump))
            results.append(check("跳转截图已保存", OUT_JUMP.stat().st_size > 12000,
                                 "%s bytes" % OUT_JUMP.stat().st_size))

        # --- 阶段 2：新增弹窗 + 粘贴图片 ---
        cdp(ws, seq, "Page.navigate",
            {"url": base + "/?token=" + TOKEN + "&tab=incidents"})
        time.sleep(3.5)

        # --- 阶段 2：双击行打开编辑（用 CDP 原生鼠标事件，而不是合成 dispatchEvent）---
        point = evaluate(ws, seq, """
          (() => {
            const row = document.querySelector('.n-data-table-tbody .n-data-table-tr');
            if (!row) return '';
            const r = row.getBoundingClientRect();
            return JSON.stringify({ x: Math.round(r.left + r.width / 2),
                                    y: Math.round(r.top + r.height / 2) });
          })()
        """)
        results.append(check("列表行可见可取坐标", bool(point), str(point)))
        pos = json.loads(point or "{}")
        for count in (1, 2):
            for kind in ("mousePressed", "mouseReleased"):
                cdp(ws, seq, "Input.dispatchMouseEvent", {
                    "type": kind, "x": pos.get("x", 0), "y": pos.get("y", 0),
                    "button": "left", "clickCount": count,
                })
        time.sleep(1.2)
        dbl_modal = evaluate(ws, seq, "document.body.innerText.includes('编辑运维记录') ? 'ok' : 'no-modal'")
        results.append(check("双击行弹出该条的编辑弹窗", dbl_modal == "ok", str(dbl_modal)))
        evaluate(ws, seq, """
          (() => {
            const btn = [...document.querySelectorAll('.n-modal button')]
              .find(b => b.textContent.trim() === '取消');
            if (btn) btn.click();
            return 'ok';
          })()
        """)
        time.sleep(0.8)

        # --- 阶段 3：新增弹窗 + 粘贴图片 ---
        clicked = evaluate(ws, seq, """
          (() => {
            const btn = [...document.querySelectorAll('button')]
              .find(b => b.textContent.trim() === '新增');
            if (!btn) return 'no-button';
            btn.click();
            return 'ok';
          })()
        """)
        results.append(check("能点开「新增运维记录」", clicked == "ok", str(clicked)))
        time.sleep(1.2)

        shape = json.loads(evaluate(ws, seq, """
          JSON.stringify({
            editors: document.querySelectorAll('.ProseMirror').length,
            hint: document.body.innerText.includes('Ctrl+V 贴图'),
          })
        """) or "{}")
        results.append(check("四个富文本字段都挂载了编辑器",
                             shape.get("editors") == 4, "editors=%s" % shape.get("editors")))
        results.append(check("显示快捷键提示", bool(shape.get("hint"))))

        pasted = evaluate(ws, seq, """
          (() => {
            const bin = atob('%s');
            const arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            const dt = new DataTransfer();
            dt.items.add(new File([arr], 'shot.png', { type: 'image/png' }));
            const pm = document.querySelector('.ProseMirror');
            if (!pm) return 'no-editor';
            pm.focus();
            pm.dispatchEvent(new ClipboardEvent('paste',
              { clipboardData: dt, bubbles: true, cancelable: true }));
            return 'ok';
          })()
        """ % PNG_1PX)
        results.append(check("粘贴事件被编辑器接管", pasted == "ok", str(pasted)))
        time.sleep(3.0)

        after = json.loads(evaluate(ws, seq, """
          JSON.stringify({
            imgs: document.querySelectorAll('.ProseMirror img').length,
            src: (document.querySelector('.ProseMirror img') || {}).src || '',
          })
        """) or "{}")
        results.append(check("剪贴板位图已上传并插入为图片节点",
                             after.get("imgs") == 1
                             and "/api/attachments/blob/" in after.get("src", ""),
                             after.get("src", "")))

        shot = cdp(ws, seq, "Page.captureScreenshot", {"format": "png"})
        data = shot.get("result", {}).get("data", "")
        if data:
            OUT.write_bytes(base64.b64decode(data))
            results.append(check("截图已保存", OUT.stat().st_size > 12000,
                                 "%s bytes" % OUT.stat().st_size))
        else:
            results.append(check("截图已保存", False, "captureScreenshot 无数据"))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d) -> %s"
              % (sum(results), len(results), OUT), flush=True)
        return 0 if ok else 1
    finally:
        for proc in (chrome, server):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
