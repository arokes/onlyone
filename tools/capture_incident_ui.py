# -*- coding: utf-8 -*-
"""运维记录富文本的界面渲染验证：真实服务 + Chrome headless 截图 + DOM 断言。

用法：py -3 tools\\capture_incident_ui.py

断言
----
* 表格「现象」列显示纯文本 + 「〔N 图〕」，不出现 ``"type":"doc"`` 之类的 JSON 残片。
* 截图能正常产出（页面未白屏 / 未抛错）。
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
from datetime import datetime, timedelta
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT_DIR = ROOT / "docs" / "screenshots"
TOKEN = "ui-rich-20261123"
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


def http(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def doc(paragraphs: list[str], image_url: str = "") -> str:
    content: list[dict] = [{"type": "paragraph",
                            "content": [{"type": "text", "text": p}]} for p in paragraphs]
    if image_url:
        content.append({"type": "image", "attrs": {"src": image_url}})
    return json.dumps({"type": "doc", "content": content}, ensure_ascii=False)


def main() -> int:
    if not Path(CHROME).exists():
        print("FAIL 未找到 Chrome：%s" % CHROME)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="onlyone_ui_rich_"))
    profile = Path(tempfile.mkdtemp(prefix="onlyone_chrome_rich_"))
    port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    env["ONLYONE_DATA_DIR"] = str(data_dir)
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(160):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")

        # 建一个空索引目录，跳过「首次启动向导」遮罩，截图才是干净界面
        index_dir = data_dir / "index_demo"
        index_dir.mkdir(parents=True, exist_ok=True)
        http(base, "/api/roots", "POST", {"path": str(index_dir), "kind": "local"})

        up = http(base, "/api/attachments/images", "POST",
                  {"data": "data:image/png;base64," + PNG_1PX, "name": "shot.png"})
        http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d %H:%M"),
            "category": "BUG", "system": "SAP-PP",
            "proposer": "张工", "company": "信息中心",
            "symptom": doc(["MRP 需求较昨日翻倍，计划订单被重复生成"], up["url"]),
            "root_cause": doc(["计划边际码被改成 1，需求合并失效"]),
            "action": doc(["恢复边际码后重跑 MRP，重复计划单已清理"]),
            "note": doc(["详见截图"]),
            "status": "open", "duration_min": 45,
        }})
        http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": "2026-11-21 14:10", "category": "优化", "system": "SAP-MM",
            "proposer": "李工", "company": "业务一部",
            "symptom": "库存报表口径不一致（纯文本老数据）",
            "root_cause": "视图版本过旧", "status": "closed", "duration_min": 20,
        }})
        time.sleep(1.0)

        url = base + "/?token=" + TOKEN + "&tab=incidents"
        shot = OUT_DIR / "07-incidents-rich.png"
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
                        "--window-size=1600,900", "--virtual-time-budget=9000",
                        "--user-data-dir=" + str(profile),
                        "--screenshot=" + str(shot), url],
                       timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok_shot = shot.exists() and shot.stat().st_size > 12000
        print(("PASS " if ok_shot else "FAIL ") + "screenshot 07-incidents-rich.png  %s bytes"
              % (shot.stat().st_size if shot.exists() else 0))

        dom_proc = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
             "--user-data-dir=" + str(profile), "--virtual-time-budget=9000", "--dump-dom", url],
            timeout=120, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        dom = dom_proc.stdout.decode("utf-8", "ignore")
        checks = [
            ("表格显示「〔1 图〕」标记", "〔1 图〕" in dom),
            ("表格显示现象纯文本", "MRP 需求较昨日翻倍" in dom),
            ("DOM 中无 JSON 残片", '"type":"doc"' not in dom and "&quot;type&quot;:&quot;doc&quot;" not in dom),
            ("老纯文本记录正常显示", "纯文本老数据" in dom),
            ("提出人 / 公司 列已渲染", "张工" in dom and "信息中心" in dom),
            ("类型列已渲染（表头 + 中文原值）",
             "类型" in dom and ">BUG<" in dom and ">优化<" in dom),
            ("超期未关闭行带标黄样式", "row-overdue" in dom),
            ("侧栏不再有工作日志入口", "工作日志" not in dom),
        ]
        results = [ok_shot] + [ok for _, ok in checks]
        for label, ok in checks:
            print(("PASS " if ok else "FAIL ") + label)
        print(("ALL PASS" if all(results) else "SOME FAILED")
              + "  (%d/%d) -> %s" % (sum(results), len(results), shot), flush=True)
        return 0 if all(results) else 1
    finally:
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
