# -*- coding: utf-8 -*-
"""OnlyOne M1 回归脚本（自带后端，不依赖已运行的实例，可反复运行）。

验证：状态接口 / 根目录 CRUD / 扫描 / 两字中文检索降级 / 子目录文件 / DPAPI 往返。
脚本会清理历史遗留的 onlyone_smoke_* 测试目录记录，成功与失败都不污染数据库。
用法：py -3 tools\\smoke_test.py
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
sys.path.insert(0, str(BACKEND))
from onlyone.db import connect, encrypt, decrypt  # noqa: E402

SMOKE_MARK = "%onlyone_smoke_%"


def http(base: str, path: str, token: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def purge_stale_roots() -> int:
    """清掉上一次中途失败留下的、指向已删除临时目录的记录。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT id FROM roots WHERE path LIKE ?", (SMOKE_MARK,)).fetchall()
        for row in rows:
            rid = row["id"]
            for f in conn.execute("SELECT id FROM files WHERE root_id=?", (rid,)).fetchall():
                conn.execute("DELETE FROM fts_entries WHERE entry_id IN "
                             "(SELECT id FROM entries WHERE type='file' AND ref_id=?)", (f["id"],))
                conn.execute("DELETE FROM entries WHERE type='file' AND ref_id=?", (f["id"],))
            conn.execute("DELETE FROM files WHERE root_id=?", (rid,))
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def main() -> int:
    purged = purge_stale_roots()
    if purged:
        print("CLEAN 历史遗留测试目录   ", purged)

    tmp = Path(tempfile.mkdtemp(prefix="onlyone_smoke_"))
    rid: int | None = None
    try:
        (tmp / "运维故障处理记录.txt").write_text("MariaDB disaster recovery", encoding="utf-8")
        (tmp / "Q3物料需求汇总.xlsx").write_text("placeholder", encoding="utf-8")
        sub = tmp / "子目录"
        sub.mkdir()
        (sub / "周报.txt").write_text("weekly", encoding="utf-8")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        token = "smoke-token-20261106"
        env = dict(os.environ)
        env["ONLYONE_TOKEN"] = token
        proc = subprocess.Popen(
            [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
            cwd=str(BACKEND), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = "http://127.0.0.1:%d" % port
        try:
            for _ in range(120):
                try:
                    with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(0.15)
            else:
                raise RuntimeError("后端未就绪，检查 data/onlyone.log")

            st = http(base, "/api/meta/status", token)
            assert st["ok"] and st["app"] == "OnlyOne", st
            print("PASS status           ", st["counts"])

            r = http(base, "/api/roots", token, "POST", {"path": str(tmp), "kind": "anyshare_sync"})
            rid = r["id"]
            print("PASS add_root         ", rid)

            scan = http(base, "/api/roots/%d/scan" % rid, token, "POST", {})
            assert scan["scanned"] >= 3 and scan["added"] >= 3, scan
            print("PASS scan             ", {k: scan[k] for k in
                                            ("scanned", "added", "updated", "removed", "failed", "seconds")})

            res = http(base, "/api/search?q=" + urllib.parse.quote("运维"), token)
            assert res["mode"] == "like_fallback", res
            assert any("运维" in i["title"] for i in res["items"]), res
            print("PASS 两字中文 LIKE 兜底", len(res["items"]), "条命中")

            long = http(base, "/api/search?q=" + urllib.parse.quote("故障处理"), token)
            assert long["mode"] == "fts5_trigram", long
            print("PASS 四字中文 FTS5    ", len(long["items"]), "条命中")

            sub_res = http(base, "/api/search?q=" + urllib.parse.quote("周报"), token)
            assert sub_res["items"], sub_res
            print("PASS 子目录文件       ", len(sub_res["items"]), "条命中")

            blob = encrypt("p@ssw0rd-测试")
            assert decrypt(blob) == "p@ssw0rd-测试" and b"p@ssw0rd" not in blob
            print("PASS DPAPI 往返       ", len(blob), "字节密文")

            http(base, "/api/roots/%d" % rid, token, "DELETE")
            rid = None
            print("PASS del_root")
            print("ALL PASS")
            return 0
        finally:
            # 中途断言失败也要把本次登记的临时目录撤掉
            if rid is not None:
                try:
                    http(base, "/api/roots/%d" % rid, token, "DELETE")
                except Exception:  # noqa: BLE001
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        purge_stale_roots()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
