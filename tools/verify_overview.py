# -*- coding: utf-8 -*-
"""概览接口回归（2026-11-25）：`GET /api/overview` 的字段、口径与降级。

用法：py -3 tools\\verify_overview.py

覆盖
----
* 空库：HTTP 200、计数全 0、列表为空、warnings 为空；
* 有数据：counts 与实际行数一致，recent_files 按 mtime 倒序且 title 缺失时回退文件名；
* 超期：按 occurred_at 天数判定，恰好阈值内不算超期，脏日期不 500 也不计入超期；
* 参数：days 改变判定结果；limit 越界被夹取而不是 422。

脚本自起后端（独立临时数据目录），不读写 data\\onlyone.db。
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
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOKEN = "overview-verify-20261125"

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


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="onlyone_overview_"))
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

        # ---------------------------------------------------- 空库
        empty = http(base, "/api/overview")
        check("空库返回 200 且结构完整",
              all(k in empty for k in ("generated_at", "params", "counts", "recent_files",
                                       "open_incidents", "recent_notes", "storage",
                                       "scan", "warnings")))
        check("空库计数全 0",
              all(empty["counts"].get(k, -1) == 0 for k in
                  ("files", "entries", "notes", "projects", "links",
                   "incidents_open", "incidents_overdue", "failures")),
              json.dumps(empty["counts"], ensure_ascii=False))
        check("空库列表为空", empty["recent_files"] == [] and empty["open_incidents"] == [])
        check("空库无降级告警", empty["warnings"] == [], str(empty["warnings"]))

        # ---------------------------------------------------- 造数据
        # 直接写库而不是走 /api/roots：注册 root 会触发 watcher 校准扫描，
        # 把脚本伪造的文件行当成"磁盘上已消失"清掉，断言会不稳定。
        con = sqlite3.connect(str(data_dir / "onlyone.db"), timeout=30)
        con.row_factory = sqlite3.Row
        now = time.time()
        root_id = con.execute(
            "INSERT INTO roots(path, kind) VALUES(?, 'local')", (str(index_dir),)
        ).lastrowid
        files = [
            ("SAP\\MRP\\MRP 需求对比 20261124.xlsx", "xlsx", 184320, now - 3600),
            ("制度\\权限矩阵说明.docx", "docx", 98304, now - 86400),
            ("旧\\会议纪要.txt", "txt", 2048, now - 3 * 86400),
        ]
        file_ids = []
        for rel, ext, size, mtime in files:
            fid = con.execute(
                """INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime,
                                     sha256, extract_status, indexed_at)
                   VALUES(?,?,?,?,?,?,?, 'ok', datetime('now','localtime'))""",
                (root_id, rel, str(index_dir / rel.replace("\\", "_")), ext, size, mtime, ""),
            ).lastrowid
            file_ids.append(fid)
        # 只给最新那条建 entries：验证 JOIN 取标题，其余两条走文件名回退
        con.execute(
            """INSERT INTO entries(type, ref_id, title, summary, occurred_at, tags)
               VALUES('file', ?, 'MRP 需求对比 20261124', '摘要',
                      datetime('now','localtime'), '文件')""",
            (file_ids[0],),
        )
        con.commit()
        con.close()

        old_at = stamp(datetime.now() - timedelta(days=12))
        today_at = stamp(datetime.now().replace(hour=9, minute=0, second=0, microsecond=0))

        def incident(payload: dict) -> None:
            http(base, "/api/biz/incidents", "POST", {"data": payload})

        incident({"occurred_at": old_at, "category": "BUG", "system": "SAP-PP",
                  "symptom": "MRP 需求较昨日翻倍", "status": "open"})
        incident({"occurred_at": today_at, "category": "优化", "system": "SAP-MM",
                  "symptom": "库存报表口径", "status": "open"})
        incident({"occurred_at": today_at, "category": "新需求", "system": "SAP-FI",
                  "symptom": "凭证过账失败", "status": "closed"})
        incident({"occurred_at": "2026-02-31 09:00", "category": "业务方案",
                  "system": "脏数据", "symptom": "非法日期", "status": "open"})

        nb = http(base, "/api/notebooks", "POST", {"name": "验证笔记本"})
        sec = http(base, "/api/sections", "POST", {"notebook_id": nb["id"], "name": "SAP 笔记"})
        http(base, "/api/notes", "POST", {"section_id": sec["id"], "title": "MRP 边际码排查"})

        data = http(base, "/api/overview")
        counts = data["counts"]
        check("文件计数 = 3", counts["files"] == 3, str(counts["files"]))
        check("未关闭运维 = 3（含 1 条脏日期）", counts["incidents_open"] == 3,
              str(counts["incidents_open"]))
        check("超期运维 = 1", counts["incidents_overdue"] == 1, str(counts["incidents_overdue"]))

        first = data["recent_files"][0]
        check("最近文件按 mtime 倒序", first["rel_path"].startswith("SAP\\MRP"),
              first["rel_path"])
        check("有 entries 时取条目标题", first.get("title") == "MRP 需求对比 20261124"
              and first.get("entry_id") is not None, str(first.get("title")))
        second = data["recent_files"][1]
        check("无 entries 时标题回退文件名", second.get("title") == "权限矩阵说明.docx"
              and second.get("entry_id") is None, str(second.get("title")))
        check("mtime 已转本地时间串", len(str(first.get("mtime"))) == 19,
              str(first.get("mtime")))

        items = data["open_incidents"]
        check("已关闭记录不出现在待跟进列表",
              all(i["system"] != "SAP-FI" for i in items), str([i["system"] for i in items]))
        old_item = next((i for i in items if i["system"] == "SAP-PP"), None)
        check("12 天前未关闭 = 挂起 12 天且超期",
              old_item is not None and old_item["days_open"] == 12 and old_item["overdue"],
              json.dumps(old_item, ensure_ascii=False) if old_item else "缺失")
        dirty_item = next((i for i in items if i["system"] == "脏数据"), None)
        check("脏日期 days_open 为 null 且不算超期",
              dirty_item is not None and dirty_item["days_open"] is None
              and dirty_item["overdue"] is False,
              json.dumps(dirty_item, ensure_ascii=False) if dirty_item else "缺失")
        check("标题形如「系统 · 现象」",
              bool(old_item) and old_item["title"].startswith("SAP-PP · "),
              old_item["title"] if old_item else "缺失")
        check("笔记出现在概览", any(n["title"] == "MRP 边际码排查" for n in data["recent_notes"]),
              str([n["title"] for n in data["recent_notes"]]))

        # ---------------------------------------------------- 参数
        wide = http(base, "/api/overview?days=30")
        check("days=30 时同一记录不再超期", wide["counts"]["incidents_overdue"] == 0,
              str(wide["counts"]["incidents_overdue"]))
        one = http(base, "/api/overview?limit=1")
        check("limit=1 只返回 1 条", len(one["recent_files"]) == 1, str(len(one["recent_files"])))
        clamped = http(base, "/api/overview?limit=99")
        check("limit 越界被夹到 20 而不是报错", len(clamped["recent_files"]) <= 20,
              str(len(clamped["recent_files"])))
        check("正常路径无降级告警", data["warnings"] == [], str(data["warnings"]))

        storage = data.get("storage") or {}
        check("体积区块可用", storage.get("db_mb", 0) > 0, json.dumps(storage, ensure_ascii=False))
        print("\n示例返回（截断）：")
        preview = dict(data)
        preview["recent_files"] = data["recent_files"][:2]
        preview["open_incidents"] = data["open_incidents"][:2]
        print(json.dumps(preview, ensure_ascii=False, indent=2)[:1200])

        ok = all(results)
        print("\n%s  (%d/%d)" % ("ALL PASS" if ok else "SOME FAILED",
                                 sum(results), len(results)))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print("FAIL HTTP", exc.code, exc.read().decode("utf-8", "ignore")[:400])
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
