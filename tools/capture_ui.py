# -*- coding: utf-8 -*-
"""M5-6 真实界面截图：本地服务 + 演示数据 + Chrome headless 截图。

产出 ``docs/screenshots/*.png``，用于补齐此前 G-6「界面未做真实渲染验证」。
截图前后会清理所有演示数据，不污染 data/onlyone.db。
用法：py -3 tools\\capture_ui.py
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
from onlyone.db import connect  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT_DIR = ROOT / "docs" / "screenshots"
TOKEN = "ui-capture-20261106"
PREFIX = "界面演示-"


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


def purge() -> None:
    """清理历史演示数据（按前缀），保证可反复运行。"""
    conn = connect()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM roots WHERE path LIKE ?", ("%onlyone_ui_%",))]
        for rid in ids:
            for f in conn.execute("SELECT id FROM files WHERE root_id=?", (rid,)).fetchall():
                conn.execute("DELETE FROM fts_entries WHERE entry_id IN "
                             "(SELECT id FROM entries WHERE type='file' AND ref_id=?)", (f[0],))
                conn.execute("DELETE FROM entries WHERE type='file' AND ref_id=?", (f[0],))
            conn.execute("DELETE FROM files WHERE root_id=?", (rid,))
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        for table, col in (("projects", "name"), ("links", "title"),
                           ("incidents", "system"), ("credentials", "title")):
            conn.execute(f"DELETE FROM {table} WHERE {col} LIKE ?", (PREFIX + "%",))
        # 笔记的 entries / fts / 语义向量没有外键，删笔记本前先手动清
        note_ids = [r[0] for r in conn.execute(
            """SELECT n.id FROM notes n JOIN sections s ON n.section_id=s.id
               JOIN notebooks b ON s.notebook_id=b.id WHERE b.name LIKE ?""",
            (PREFIX + "%",))]
        for nid in note_ids:
            row = conn.execute("SELECT id FROM entries WHERE type='note' AND ref_id=?",
                               (nid,)).fetchone()
            if row:
                conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (row[0],))
                conn.execute("DELETE FROM semantic_index WHERE entry_id=?", (row[0],))
                conn.execute("DELETE FROM entries WHERE id=?", (row[0],))
        conn.execute("DELETE FROM notebooks WHERE name LIKE ?", (PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


def seed(base: str, tmp: Path) -> int:
    """插入一批演示数据，让截图不是空白页。"""
    # 索引目录与样例文件
    (tmp / "灾备恢复手册.txt").write_text(
        "灾备恢复手册 数据库备份与恢复流程 2026", encoding="utf-8")
    (tmp / "周会纪要.txt").write_text("灾备演练排期与负责人分工", encoding="utf-8")
    try:
        from docx import Document

        doc = Document()
        doc.add_paragraph("灾备恢复手册 数据库切换预案")
        doc.save(str(tmp / "灾备预案.docx"))
    except Exception:  # noqa: BLE001
        pass
    rid = http(base, "/api/roots", "POST",
               {"path": str(tmp), "kind": "local"})["id"]
    http(base, "/api/roots/%d/scan" % rid, "POST", {})

    http(base, "/api/biz/projects", "POST",
         {"data": {"name": PREFIX + "灾备系统升级",
                   "note": "灾备切换演练与数据库主从改造", "status": "active"}})
    http(base, "/api/biz/incidents", "POST",
         {"data": {"occurred_at": "2026-11-06 10:00", "system": PREFIX + "灾备平台",
                   "proposer": "张工", "company": "信息中心",
                   "symptom": PREFIX + "完成灾备切换演练脚本评审",
                   "duration_min": 90, "status": "closed"}})
    # 近义演示：查询「宕机」时字面无命中，但语义分组应命中这条「服务不可用」
    # （工作日志停用后，这段近义文本改由运维记录承载）
    http(base, "/api/biz/incidents", "POST",
         {"data": {"occurred_at": "2026-11-06 11:00", "system": PREFIX + "订单服务",
                   "proposer": "李工", "company": "业务一部",
                   "symptom": PREFIX + "服务不可用，需要立即处理",
                   "duration_min": 20}})
    http(base, "/api/biz/incidents", "POST",
         {"data": {"occurred_at": "2026-11-05 21:30", "system": PREFIX + "数据库",
                   "symptom": "主从复制延迟告警", "root_cause": "磁盘 IO 饱和",
                   "action": "扩容并重建复制链路", "status": "closed",
                   "duration_min": 45}})
    http(base, "/api/biz/links", "POST",
         {"data": {"title": PREFIX + "演练方案（企微文档）",
                   "url": "https://doc.weixin.qq.com/doc/demo",
                   "note": "仅登记链接，不读取文档内容"}})
    http(base, "/api/credentials", "POST",
         {"title": PREFIX + "内网数据库", "username": "ops_admin",
          "password": "demo-only-not-a-real-secret", "url": "https://db.internal.example",
          "note": "演示数据"})

    # M7 记事本演示：笔记本 → 分区 → 页面，并写入富文本
    book = http(base, "/api/notebooks", "POST", {"name": PREFIX + "工作笔记"})
    section = http(base, "/api/sections", "POST",
                   {"notebook_id": book["id"], "name": "运维分区"})
    demo = http(base, "/api/notes", "POST",
                {"section_id": section["id"], "title": PREFIX + "灾备预案"})
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1},
         "content": [{"type": "text", "text": "灾备恢复手册"}]},
        {"type": "paragraph",
         "content": [{"type": "text", "text": "数据库备份与恢复流程 2026，演练前请确认主从状态。"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "检查复制链路与磁盘 IO"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "执行切换并记录耗时"}]}]},
        ]},
    ]}
    http(base, "/api/notes/%d" % demo["id"], "PUT", {
        "title": PREFIX + "灾备预案",
        "content_json": json.dumps(doc, ensure_ascii=False),
        "is_private": False, "revision": 0,
    })
    return {"rid": rid, "note_id": demo["id"]}


def cleanup(base: str, rid: int | None) -> None:
    if rid is not None:
        try:
            http(base, "/api/roots/%d" % rid, "DELETE")
        except Exception:  # noqa: BLE001
            pass
    for kind in ("incidents", "links", "projects"):
        try:
            for row in http(base, "/api/biz/" + kind):
                blob = json.dumps(row, ensure_ascii=False)
                if PREFIX in blob:
                    http(base, "/api/biz/%s/%d" % (kind, row["id"]), "DELETE")
        except Exception:  # noqa: BLE001
            pass
    try:
        for row in http(base, "/api/credentials"):
            if PREFIX in str(row.get("title", "")):
                http(base, "/api/credentials/%d" % row["id"], "DELETE")
    except Exception:  # noqa: BLE001
        pass
    try:
        for row in http(base, "/api/notebooks"):
            if PREFIX in str(row.get("name", "")):
                # 删除笔记本的 API 会级联清理 notes 与 entries/fts/语义向量
                http(base, "/api/notebooks/%d" % row["id"], "DELETE")
    except Exception:  # noqa: BLE001
        pass


def capture(base: str, tab: str, query: str, name: str,
            profile: Path, extra: str = "") -> bool:
    url = base + "/?token=" + TOKEN + "&tab=" + tab
    if query:
        url += "&q=" + urllib.parse.quote(query)
    if extra:
        url += extra
    out = OUT_DIR / name
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
        "--window-size=1400,900", "--virtual-time-budget=9000",
        "--user-data-dir=" + str(profile),
        "--screenshot=" + str(out),
        url,
    ]
    try:
        subprocess.run(cmd, timeout=120, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except Exception as exc:  # noqa: BLE001
        print("FAIL capture", name, exc, flush=True)
        return False
    ok = out.exists() and out.stat().st_size > 12000
    print(("PASS " if ok else "FAIL ") + "screenshot %-22s %s bytes" %
          (name, out.stat().st_size if out.exists() else 0), flush=True)
    return ok


def main() -> int:
    if not Path(CHROME).exists():
        print("FAIL 未找到 Chrome：%s" % CHROME)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    purge()

    port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    tmp = Path(tempfile.mkdtemp(prefix="onlyone_ui_"))
    profile = Path(tempfile.mkdtemp(prefix="onlyone_chrome_"))
    rid = None
    results: list[bool] = []
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")

        seed_info = seed(base, tmp)
        rid = seed_info["rid"]
        # 给异步组件与 API 一点稳定时间
        time.sleep(1.0)

        # 文件名用 ASCII，避免 Chrome 命令行参数在非 UTF-8 环境踩编码坑
        shots = [
            ("search", "灾备", "01-search.png", ""),
            ("projects", "", "02-projects.png", ""),
            ("credentials", "", "03-credentials.png", ""),
            ("roots", "", "04-roots.png", ""),
            ("search", "宕机", "05-semantic.png", ""),
            ("notes", "", "06-notes.png", "&note=%d" % seed_info["note_id"]),
        ]
        for tab, query, name, extra in shots:
            results.append(capture(base, tab, query, name, profile, extra))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d) -> %s" %
              (sum(results), len(results), OUT_DIR), flush=True)
        return 0 if ok else 1
    finally:
        cleanup(base, rid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
        purge()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
