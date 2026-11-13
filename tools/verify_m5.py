# -*- coding: utf-8 -*-
"""M5 验收集：watchdog 增量索引、失败重试队列、重建索引、PyInstaller 离线启动。

自带后端进程并显式设置 ONLYONE_WATCHER=1（绕过 --no-window 的默认关闭）；
可反复运行。用法：py -3 tools\\verify_m5.py
"""
from __future__ import annotations

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

# M8 修复：本套回归启用 watcher，启动校准会扫描所有 enabled 根目录。
# 跑在真实库上时会把用户的 D:\SAP文件 这类大目录拉进来，长时间占住 _SCAN_LOCK，
# 导致脚本自己的同步扫描请求 60 秒超时。改为独立数据目录，彻底隔离。
DATA_DIR = Path(tempfile.mkdtemp(prefix="onlyone_m5_"))
os.environ["ONLYONE_DATA_DIR"] = str(DATA_DIR)

sys.path.insert(0, str(BACKEND))
from onlyone.db import connect  # noqa: E402

TOKEN = "m5-verify-token-20261106"
EXE_TOKEN = "m5-exe-token-20261106"


def http(base: str, path: str, method: str = "GET", body=None, token: str = TOKEN):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def purge_roots() -> int:
    conn = connect()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM roots WHERE path LIKE ?", ("%onlyone_m5_%",))]
        for rid in ids:
            for f in conn.execute("SELECT id FROM files WHERE root_id=?", (rid,)).fetchall():
                conn.execute("DELETE FROM fts_entries WHERE entry_id IN "
                             "(SELECT id FROM entries WHERE type='file' AND ref_id=?)", (f[0],))
                conn.execute("DELETE FROM entries WHERE type='file' AND ref_id=?", (f[0],))
            conn.execute("DELETE FROM files WHERE root_id=?", (rid,))
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    env["ONLYONE_WATCHER"] = "1"  # 在 --no-window 下显式启用 watcher
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    results: list[bool] = []
    tmp: Path | None = None
    rid = None
    exe_proc = None
    try:
        for _ in range(160):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪，检查 data/onlyone.log")

        purged = purge_roots()
        if purged:
            print("CLEAN 历史遗留测试目录", purged, flush=True)
        tmp = Path(tempfile.mkdtemp(prefix="onlyone_m5_"))

        # 1) watcher 状态与目录登记（后端就绪早于 watcher 启动，需轮询等待）
        results.append(check("watcher 已启用", wait_for(
            lambda: http(base, "/api/watcher/status")["enabled"] is True, 15)))
        rid = http(base, "/api/roots", "POST",
                   {"path": str(tmp), "kind": "local"})["id"]
        results.append(check("目录纳入监听", wait_for(
            lambda: http(base, "/api/watcher/status")["watched_roots"] >= 1, 10)))

        # 2) 新建文件 → 自动索引
        f = tmp / "灾备切换演练.txt"
        f.write_text("灾备切换演练 步骤一", encoding="utf-8")
        q1 = "/api/search?q=" + urllib.parse.quote("灾备切换演练")
        results.append(check("新建文件自动索引",
                             wait_for(lambda: bool(http(base, q1)["items"]), 25)))

        # 3) 修改文件 → 索引更新
        f.write_text("灾备切换演练 步骤二 新增标记XYZ", encoding="utf-8")
        q2 = "/api/search?q=" + urllib.parse.quote("新增标记XYZ")
        results.append(check("修改文件自动更新",
                             wait_for(lambda: bool(http(base, q2)["items"]), 25)))

        # 4) 删除文件 → 索引移除
        f.unlink()
        results.append(check("删除文件自动移除",
                             wait_for(lambda: not http(base, q2)["items"], 25)))

        # 5) 抽取失败进入重试队列，累计 3 次列入失败清单
        bad = tmp / "损坏文档.docx"
        bad.write_text("this is not a zip", encoding="utf-8")
        http(base, "/api/roots/%d/scan" % rid, "POST", {})
        fails = [x for x in http(base, "/api/failures")
                 if x["rel_path"].endswith("损坏文档.docx")]
        results.append(check("失败进入队列", bool(fails)))
        if fails:
            fid = fails[0]["file_id"]
            for _ in range(3):
                http(base, "/api/roots/%d/scan" % rid, "POST", {})
            given = http(base, "/api/failures?given_up=true")
            results.append(check("重试 3 次列入失败清单",
                                 any(x["file_id"] == fid for x in given)))
            http(base, "/api/failures/%d/retry" % fid, "POST", {})
            after = [x for x in http(base, "/api/failures") if x["file_id"] == fid]
            results.append(check("手动重试重置次数",
                                 bool(after) and after[0]["attempts"] <= 2,
                                 "attempts=%s" % (after[0]["attempts"] if after else "n/a")))
        else:
            results.append(check("重试 3 次列入失败清单", False))
            results.append(check("手动重试重置次数", False))

        # 6) 重建索引（忽略 size+mtime）
        rebuild = http(base, "/api/roots/%d/rebuild" % rid, "POST", {})
        print("REBUILD", {k: rebuild[k] for k in
                          ("scanned", "updated", "retried", "extract_failed", "seconds")}, flush=True)
        results.append(check("重建索引", rebuild["scanned"] >= 1
                             and (rebuild["updated"] + rebuild["retried"]) >= 1))
        results.append(check("watcher 有扫描记录",
                             http(base, "/api/watcher/status")["scans"] >= 1))

        # 7) PyInstaller 产物与离线启动（数据与前端都在 EXE 同级）
        exe = ROOT / "dist" / "OnlyOne" / "OnlyOne.exe"
        has_bundle = exe.exists() and (exe.parent / "frontend" / "dist" / "index.html").exists()
        results.append(check("打包产物完整", has_bundle, str(exe)))
        if has_bundle:
            port2 = free_port()
            env2 = dict(os.environ)
            env2["ONLYONE_TOKEN"] = EXE_TOKEN
            exe_proc = subprocess.Popen(
                [str(exe), "--no-window", "--port", str(port2)],
                cwd=str(exe.parent), env=env2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            base2 = "http://127.0.0.1:%d" % port2
            ready = wait_for(lambda: urllib.request.urlopen(
                base2 + "/api/meta/status", timeout=1).status == 200, 40)
            ui_ok = False
            if ready:
                try:
                    with urllib.request.urlopen(
                        base2 + "/?token=" + EXE_TOKEN, timeout=5
                    ) as resp:
                        html = resp.read().decode("utf-8", errors="replace")
                        ui_ok = resp.status == 200 and ("<div id=\"app\"" in html
                                                        or "OnlyOne" in html)
                except Exception:  # noqa: BLE001
                    pass
            results.append(check("EXE 离线启动 API", ready))
            results.append(check("EXE 静态页可访问", ui_ok))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d)" % (sum(results), len(results)),
              flush=True)
        return 0 if ok else 1
    finally:
        if rid is not None:
            try:
                http(base, "/api/roots/%d" % rid, "DELETE")
            except Exception:  # noqa: BLE001
                pass
        if exe_proc is not None:
            exe_proc.terminate()
            try:
                exe_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                exe_proc.kill()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        purge_roots()
        shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
