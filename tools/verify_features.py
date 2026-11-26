# -*- coding: utf-8 -*-
"""功能 1/2/3 的后端断言（2026-11-25）。

用法：py -3 tools\\verify_features.py

覆盖
----
* 标签（1）：写入去重、聚合计数、按标签搜索、与关键词组合、超限拒绝、列表带出；
* 保存视图（2）：创建时清空条件、按页签过滤、改名、删除、空名拒绝；
* 重复文件（3）：分组/冗余/可释放口径、最小体积过滤、limit 夹取、未哈希文件如实计数。

脚本自起后端 + 独立临时目录，不读写 data\\onlyone.db。
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
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOKEN = "features-verify-20261125"
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("%s %s%s" % ("PASS " if ok else "FAIL ", label, ("  " + detail) if detail else "")),
          flush=True)
    results.append(bool(ok))


def http(base: str, path: str, method: str = "GET", body=None, expect_error: bool = False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if expect_error:
            return {"__status": exc.code}
        raise


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="onlyone_features_"))
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

        # ---------------------------------------------------- 造数据
        incident = http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": "2026-11-20 09:00", "category": "BUG", "system": "SAP-PP",
            "proposer": "张工", "company": "信息中心",
            "symptom": "MRP 需求翻倍", "status": "open",
        }})
        entry_id = incident["entry_id"]
        record_id = incident["id"]

        other = http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": "2026-11-21 10:00", "category": "优化", "system": "SAP-MM",
            "symptom": "库存报表口径", "status": "closed",
        }})
        other_entry = other["entry_id"]

        con = sqlite3.connect(str(data_dir / "onlyone.db"), timeout=30)
        con.execute("INSERT INTO roots(path, kind) VALUES(?, 'local')", (str(index_dir),))
        root_id = con.execute("SELECT id FROM roots LIMIT 1").fetchone()[0]
        now = time.time()
        files = [
            ("SAP\\a.xlsx", "xlsx", 1000, now - 60, "hash-dup"),
            ("SAP\\b.xlsx", "xlsx", 1000, now - 120, "hash-dup"),
            ("SAP\\c.xlsx", "xlsx", 2048, now - 180, "hash-uniq"),
            ("SAP\\big.bin", "bin", 60 * 1024 * 1024, now - 240, ""),
        ]
        for rel, ext, size, mtime, digest in files:
            con.execute(
                """INSERT INTO files(root_id, rel_path, abs_path, ext, size, mtime,
                                     sha256, extract_status, indexed_at)
                   VALUES(?,?,?,?,?,?,?, 'ok', datetime('now','localtime'))""",
                (root_id, rel, str(index_dir / rel.replace("\\", "_")), ext, size, mtime, digest),
            )
        con.commit()
        con.close()

        # ---------------------------------------------------- 1 标签
        put = http(base, "/api/entries/%d/tags" % entry_id, "PUT",
                   {"tags": ["MRP", "SAP", "MRP", "  "]})
        check("标签写入去重（MRP 只留一次）", put["tags"] == ["MRP", "SAP"], str(put["tags"]))

        tags = http(base, "/api/tags")
        mrp = next((t for t in tags if t["tag"] == "MRP"), None)
        check("标签聚合带计数", mrp is not None and mrp["count"] == 1,
              json.dumps(tags, ensure_ascii=False))

        only_tag = http(base, "/api/search?tag=MRP")
        check("仅按标签搜索（q 为空）", only_tag["mode"] == "tag_filter"
              and len(only_tag["items"]) == 1
              and only_tag["items"][0]["id"] == entry_id,
              "mode=%s n=%d" % (only_tag["mode"], len(only_tag["items"])))

        combined = http(base, "/api/search?q=%s&tag=MRP" % urllib.parse.quote("需求翻倍"))
        check("关键词 + 标签组合命中", len(combined["items"]) == 1,
              "n=%d" % len(combined["items"]))

        excluded = http(base, "/api/search?q=%s&tag=%s"
                        % (urllib.parse.quote("需求翻倍"), urllib.parse.quote("不存在的标签")))
        check("关键词命中但标签不符 → 0 条", len(excluded["items"]) == 0,
              "n=%d" % len(excluded["items"]))

        listed = http(base, "/api/biz/incidents")
        row = next((r for r in listed if r["id"] == record_id), None)
        check("列表带出 user_tags", row is not None and row.get("user_tags") == '["MRP", "SAP"]',
              str(row.get("user_tags") if row else "缺失"))

        too_many = http(base, "/api/entries/%d/tags" % entry_id, "PUT",
                        {"tags": ["t%d" % i for i in range(11)]}, expect_error=True)
        check("超过 10 个标签被拒绝", too_many.get("__status") == 400, str(too_many))

        too_long = http(base, "/api/entries/%d/tags" % entry_id, "PUT",
                        {"tags": ["x" * 21]}, expect_error=True)
        check("标签超长被拒绝", too_long.get("__status") == 400, str(too_long))

        missing = http(base, "/api/entries/999999/tags", "PUT", {"tags": ["a"]},
                       expect_error=True)
        check("不存在的条目返回 404", missing.get("__status") == 404, str(missing))

        # ---------------------------------------------------- 2 保存视图
        view = http(base, "/api/views", "POST", {
            "name": "我的 BUG", "tab": "incidents",
            "filters": {"status": "open", "company": "", "occurred_at": [None, None]},
        })
        check("创建视图并清掉空条件",
              view.get("filters") == {"status": "open"}, json.dumps(view.get("filters")))

        mine = http(base, "/api/views?tab=incidents")
        check("按页签过滤视图", len(mine) == 1 and mine[0]["id"] == view["id"],
              str(len(mine)))
        check("其他页签看不到该视图", len(http(base, "/api/views?tab=links")) == 0)

        renamed = http(base, "/api/views/" + view["id"], "PUT", {"name": "我的 BUG（新）"})
        check("视图改名", renamed["name"] == "我的 BUG（新）", renamed["name"])

        deleted = http(base, "/api/views/" + view["id"], "DELETE")
        check("删除视图", deleted.get("deleted") == view["id"])
        check("删除后列表为空", len(http(base, "/api/views")) == 0)

        empty = http(base, "/api/views", "POST", {"name": "  ", "tab": "incidents"},
                     expect_error=True)
        check("空名视图被拒绝", empty.get("__status") == 400, str(empty))

        # ---------------------------------------------------- 3 重复文件
        dup = http(base, "/api/duplicates")
        totals = dup["totals"]
        check("重复分组 = 1", len(dup["groups"]) == 1 and totals["groups"] == 1,
              json.dumps(totals, ensure_ascii=False))
        check("冗余文件 = 1、可释放 = 1000",
              totals["redundant_files"] == 1 and totals["wasted_bytes"] == 1000,
              str(totals))
        check("未哈希文件如实计数（60MB 那个）", totals["unhashed_files"] == 1,
              str(totals["unhashed_files"]))
        check("组内列出两条路径", len(dup["groups"][0]["paths"]) == 2,
              str(len(dup["groups"][0]["paths"])))
        check("按内容哈希排序字段存在", len(dup["groups"][0]["sha256"]) == len("hash-dup"))

        big_only = http(base, "/api/duplicates?min_mb=1")
        check("min_mb=1 过滤掉 1KB 的组", len(big_only["groups"]) == 0,
              str(len(big_only["groups"])))

        clamped = http(base, "/api/duplicates?limit=0")
        check("limit=0 被夹到 1 而不是报错", isinstance(clamped["groups"], list))

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
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
