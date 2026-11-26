# -*- coding: utf-8 -*-
"""概览页界面验证：真实服务 + Chrome headless 截图 + DOM 断言。

用法：py -3 tools\\capture_overview_ui.py

断言
----
* 不带 `?tab=` 打开时落在「概览」，四个区块都在；
* 超期运维行带 `overdue` 标记，摘要给出超期条数；
* 统计条 5 格齐全；
* `?tab=search` 深链接仍落在搜索页（默认页变更没影响深链接）；
* 截图产出且体积正常（未白屏）。

脚本使用独立临时数据目录，不读写 data\\onlyone.db。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT_DIR = ROOT / "docs" / "screenshots"
SHOT = OUT_DIR / "24-overview.png"
TOKEN = "overview-ui-20261125"

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("%s %s%s" % ("PASS " if ok else "FAIL ", label, ("  " + detail) if detail else "")),
          flush=True)
    results.append(bool(ok))


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


def dump_dom(url: str, profile: Path) -> str:
    proc = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
         "--user-data-dir=" + str(profile), "--virtual-time-budget=9000", "--dump-dom", url],
        timeout=120, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc.stdout.decode("utf-8", "ignore")


def main() -> int:
    if not Path(CHROME).exists():
        print("FAIL 未找到 Chrome：%s" % CHROME)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="onlyone_overview_ui_"))
    profile = Path(tempfile.mkdtemp(prefix="onlyone_chrome_overview_"))
    index_dir = data_dir / "idx"
    index_dir.mkdir(parents=True, exist_ok=True)
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
        for _ in range(200):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")

        # 先落一条 root，App 启动时的 status 就不会弹首次向导遮罩
        con = sqlite3.connect(str(data_dir / "onlyone.db"), timeout=30)
        now = time.time()
        root_id = con.execute(
            "INSERT INTO roots(path, kind) VALUES(?, 'local')", (str(index_dir),)
        ).lastrowid
        docs = [
            ("SAP\\MRP\\MRP 需求对比 20261124.xlsx", "xlsx", 184320, now - 1800),
            ("制度\\权限矩阵说明.docx", "docx", 98304, now - 6 * 3600),
            ("SAP\\笔记\\季度回顾.txt", "txt", 4096, now - 26 * 3600),
            ("旧\\会议纪要.txt", "txt", 2048, now - 4 * 86400),
        ]
        first_id = None
        for rel, ext, size, mtime in docs:
            fid = con.execute(
                """INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime,
                                     sha256, extract_status, indexed_at)
                   VALUES(?,?,?,?,?,?,?, 'ok', datetime('now','localtime'))""",
                (root_id, rel, str(index_dir / rel.replace("\\", "_")), ext, size, mtime, ""),
            ).lastrowid
            if first_id is None:
                first_id = fid
        con.execute(
            """INSERT INTO entries(type, ref_id, title, summary, occurred_at, tags)
               VALUES('file', ?, 'MRP 需求对比 20261124', '摘要',
                      datetime('now','localtime'), '文件')""",
            (first_id,),
        )
        con.commit()
        con.close()

        old_at = (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d %H:%M")
        today_at = datetime.now().replace(hour=9, minute=10, second=0,
                                          microsecond=0).strftime("%Y-%m-%d %H:%M")
        for payload in (
            {"occurred_at": old_at, "category": "BUG", "system": "SAP-PP",
             "proposer": "张工", "company": "信息中心",
             "symptom": "MRP 需求较昨日翻倍，计划订单重复生成", "status": "open",
             "duration_min": 45},
            {"occurred_at": today_at, "category": "优化", "system": "SAP-MM",
             "proposer": "李工", "company": "业务一部",
             "symptom": "库存报表口径不一致", "status": "open", "duration_min": 20},
            {"occurred_at": today_at, "category": "业务方案", "system": "SAP-FI",
             "proposer": "王工", "company": "信息中心",
             "symptom": "凭证过账批次方案确认", "status": "closed", "duration_min": 30},
        ):
            http(base, "/api/biz/incidents", "POST", {"data": payload})

        nb = http(base, "/api/notebooks", "POST", {"name": "SAP 笔记"})
        sec = http(base, "/api/sections", "POST", {"notebook_id": nb["id"], "name": "MRP"})
        http(base, "/api/notes", "POST", {"section_id": sec["id"], "title": "MRP 边际码排查"})
        http(base, "/api/notes", "POST", {"section_id": sec["id"], "title": "物料主数据字段对照"})

        url = base + "/?token=" + TOKEN
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
                        "--window-size=1600,950", "--virtual-time-budget=9000",
                        "--user-data-dir=" + str(profile),
                        "--screenshot=" + str(SHOT), url],
                       timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok_shot = SHOT.exists() and SHOT.stat().st_size > 20000
        check("截图产出", ok_shot,
              "%s bytes" % (SHOT.stat().st_size if SHOT.exists() else 0))

        dom = dump_dom(url, profile)
        check("默认落在概览（无 ?tab=）", "概览" in dom and "待跟进运维" in dom)
        check("四个区块都在",
              all(t in dom for t in ("最近改动", "待跟进运维", "最近笔记", "系统状态")))
        check("统计条 5 格齐全",
              all(t in dom for t in ("索引文件", "全库条目", "未关闭运维", "超期未处理", "抽取失败")))
        check("超期行带 overdue 标记", "home-row overdue" in dom)
        check("摘要给出超期条数", "条超期" in dom, "")
        check("最近改动显示文件名", "MRP 需求对比 20261124" in dom)
        check("待跟进只列未关闭", "凭证过账批次方案确认" not in dom)
        check("笔记区块有内容", "MRP 边际码排查" in dom)

        search_dom = dump_dom(base + "/?token=" + TOKEN + "&tab=search", profile)
        check("?tab=search 深链接仍落在搜索页",
              "待跟进运维" not in search_dom and "搜索文件正文" in search_dom)
        check("搜索页无关键词时是中性空态",
              "输入关键词开始搜索" in search_dom
              and "还没有可搜索的内容" not in search_dom
              and "去添加索引目录" not in search_dom)

        ok = all(results)
        print("\n%s  (%d/%d) -> %s" % ("ALL PASS" if ok else "SOME FAILED",
                                       sum(results), len(results), SHOT))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
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
