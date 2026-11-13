# -*- coding: utf-8 -*-
"""M4 验收集：DPAPI 凭据 CRUD、明文不落盘/日志/响应、敏感排除、幂等清理。

自带后端进程，不依赖已运行实例；可反复运行（用 ``M4验证-`` 前缀幂等清理）。
用法：py -3 tools\\verify_m4.py

注意：「换 Windows 账户无法解密」无法在自动化脚本里真实切换账户，
本脚本验证的是等价的两个事实：密文不含明文、损坏/非本上下文密文被拒绝解密。
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
sys.path.insert(0, str(BACKEND))
from onlyone.db import DATA_DIR, DB_PATH, connect, decrypt  # noqa: E402

PREFIX = "M4验证-"
SECRET = "p@ssw0rd-测试密码-2026"
SECRET2 = "新口令-日本語-2026"
TOKEN = "m4-verify-token-20261106"


def http(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def expect_400(base: str, path: str) -> bool:
    try:
        http(base, path)
        return False
    except urllib.error.HTTPError as exc:
        return exc.code == 400


def purge_credentials(base: str) -> int:
    removed = 0
    try:
        for row in http(base, "/api/credentials"):
            if PREFIX in str(row.get("title", "")):
                http(base, "/api/credentials/%d" % row["id"], "DELETE")
                removed += 1
    except Exception:  # noqa: BLE001
        pass
    return removed


def purge_roots() -> int:
    conn = connect()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM roots WHERE path LIKE ?", ("%onlyone_m4_%",))]
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    results: list[bool] = []
    tmp: Path | None = None
    rid = None
    cid = None
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.15)
        else:
            raise RuntimeError("后端未就绪，检查 data/onlyone.log")

        purged = purge_credentials(base) + purge_roots()
        if purged:
            print("CLEAN 历史遗留记录", purged, flush=True)

        # 1) 创建凭据
        created = http(base, "/api/credentials", "POST", {
            "title": PREFIX + "内网数据库",
            "username": "admin_m4",
            "password": SECRET,
            "url": "https://db.internal.example/console",
            "note": "M4 验收备注",
        })
        cid = created["id"]
        print("CREATED credential id=%s" % cid, flush=True)

        # 2) 列表：账号常显（M8 起的新契约），密码 / 链接 / 备注仍只返回掩码
        listing = http(base, "/api/credentials")
        target = next(r for r in listing if r["id"] == cid)
        raw = json.dumps(listing, ensure_ascii=False)
        results.append(check("列表不含密码明文与备注",
                             SECRET not in raw and "M4 验收备注" not in raw))
        results.append(check("账号在列表中常显",
                             target["username"] == "admin_m4"
                             and target["has_username"] is True))
        results.append(check("列表返回掩码", target["password"] == "••••••••"
                             and target["has_password"] is True))

        # 3) 按需 reveal
        rv = http(base, "/api/credentials/%d/reveal?field=password" % cid)
        results.append(check("按需解密正确", rv["value"] == SECRET))
        results.append(check("reveal 字段白名单", expect_400(
            base, "/api/credentials/%d/reveal?field=title" % cid)))

        # 4) 更新（口令轮换 + 清空 url）
        http(base, "/api/credentials/%d" % cid, "PUT",
             {"password": SECRET2, "url": ""})
        rv2 = http(base, "/api/credentials/%d/reveal?field=password" % cid)
        rv_url = http(base, "/api/credentials/%d/reveal?field=url" % cid)
        after = next(r for r in http(base, "/api/credentials") if r["id"] == cid)
        results.append(check("更新后解密正确", rv2["value"] == SECRET2))
        results.append(check("字段清空生效", rv_url["value"] == "" and after["has_url"] is False))

        # 5) 数据库 BLOB 不含明文（db / 导出等价面）
        leaks = []
        conn = sqlite3.connect(str(DB_PATH))
        try:
            for row in conn.execute(
                "SELECT title,username_enc,password_enc,url_enc,note_enc FROM credentials"
            ):
                title = str(row[0])
                if SECRET in title or SECRET2 in title:
                    leaks.append("title")
                for blob in row[1:]:
                    if blob and (SECRET.encode("utf-8") in bytes(blob)
                                 or SECRET2.encode("utf-8") in bytes(blob)):
                        leaks.append("blob")
        finally:
            conn.close()
        results.append(check("数据库无明文", not leaks, str(leaks)))

        # 6) 日志与崩溃报告目录无明文
        log_hits = []
        for p in (DATA_DIR / "onlyone.log", DATA_DIR / "onlyone.log.1"):
            if p.exists() and (SECRET in p.read_text(encoding="utf-8", errors="replace")
                               or SECRET2 in p.read_text(encoding="utf-8", errors="replace")):
                log_hits.append(p.name)
        results.append(check("日志无明文", not log_hits, str(log_hits)))

        # 7) 损坏 / 非本上下文密文必须拒绝解密，绝不返回猜测值
        rejected = False
        try:
            decrypt(b"not-a-dpapi-blob")
        except OSError:
            rejected = True
        results.append(check("异常密文拒绝解密", rejected))

        # 8) M4-3：敏感文件不进 FTS，且统计可感知
        tmp = Path(tempfile.mkdtemp(prefix="onlyone_m4_"))
        (tmp / "系统密码清单.txt").write_text("admin / " + SECRET, encoding="utf-8")
        (tmp / "运维说明.txt").write_text("灾备恢复手册 数据库备份", encoding="utf-8")
        rid = http(base, "/api/roots", "POST",
                   {"path": str(tmp), "kind": "local"})["id"]
        scan = http(base, "/api/roots/%d/scan" % rid, "POST", {})
        print("SCAN", {k: scan[k] for k in ("scanned", "extracted", "sensitive")}, flush=True)
        results.append(check("敏感文件计数可见", scan.get("sensitive", 0) >= 1))
        secret_hits = http(base, "/api/search?q=" + urllib.parse.quote("密码"))
        results.append(check("敏感文件不进 FTS",
                             not any("密码" in i["title"] for i in secret_hits["items"])))
        normal_hits = http(base, "/api/search?q=" + urllib.parse.quote("灾备恢复手册"))
        results.append(check("普通文件仍可检索", len(normal_hits["items"]) >= 1))

        # 9) 凭据不进统一条目（FTS 中检索不到口令）
        fts_hits = http(base, "/api/search?q=" + urllib.parse.quote(SECRET2))
        results.append(check("凭据不进 FTS", not fts_hits["items"]))

        # 10) 删除
        http(base, "/api/credentials/%d" % cid, "DELETE")
        cid = None
        results.append(check("删除凭据", not any(
            r["id"] == created["id"] for r in http(base, "/api/credentials"))))

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
        if cid is not None:
            try:
                http(base, "/api/credentials/%d" % cid, "DELETE")
            except Exception:  # noqa: BLE001
                pass
        try:
            purge_credentials(base)
        except Exception:  # noqa: BLE001
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        purge_roots()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
