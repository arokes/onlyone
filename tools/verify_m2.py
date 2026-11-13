# -*- coding: utf-8 -*-
"""M2 验收集：真实 Office 文件 → 抽取 → 扫描 → 中文检索 → 只读预览 → 敏感排除。

自带后端进程，不依赖已运行实例；可反复运行，成功与失败都会清理临时目录与数据库记录。
用法：py -3 tools\\verify_m2.py
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
from onlyone.extractor import extract  # noqa: E402

MARKER = "灾备恢复手册"          # 六字词 → FTS5 trigram
SHORT = "灾备"                   # 两字词 → LIKE 兜底
SMOKE_MARK = "%onlyone_m2_%"
TOKEN = "m2-verify-token-20261106"


def http(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def purge_stale_roots() -> int:
    conn = connect()
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM roots WHERE path LIKE ?", (SMOKE_MARK,))]
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


def _wps_app(progid):
    import win32com.client

    app = win32com.client.DispatchEx(progid)
    try:
        app.Visible = False
    except Exception:  # noqa: BLE001
        pass
    try:
        app.DisplayAlerts = 0
    except Exception:  # noqa: BLE001
        pass
    return app


def make_samples(tmp: Path) -> dict[str, Path]:
    """生成 doc/docx/xls/xlsx/ppt/pptx/pdf/txt 各一份，全部含同一检索词。"""
    out: dict[str, Path] = {}

    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation

    p = tmp / "灾备手册.docx"
    d = Document()
    d.add_paragraph(MARKER + " 数据库备份与恢复流程 2026")
    t = d.add_table(rows=2, cols=2)
    for cell, value in zip((t.cell(0, 0), t.cell(0, 1), t.cell(1, 0), t.cell(1, 1)),
                           ("系统", "负责人", "数据库", "张三")):
        cell.text = value
    d.save(str(p))
    out["docx"] = p

    p = tmp / "灾备清单.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "清单"
    ws.append(["系统", "负责人"])
    ws.append([MARKER, "李四"])
    wb.save(str(p))
    out["xlsx"] = p

    p = tmp / "灾备演示.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = MARKER + " 数据库"
    prs.save(str(p))
    out["pptx"] = p

    p = tmp / "灾备说明.txt"
    p.write_text(MARKER + " 数据库备份与恢复流程（纯文本）", encoding="utf-8")
    out["txt"] = p

    # 旧格式与 PDF 由本机 WPS COM 生成（无需用户提供样例）
    import pythoncom

    pythoncom.CoInitialize()
    try:
        app = _wps_app("KWPS.Application")
        doc = app.Documents.Add()
        doc.Content.Text = MARKER + " 数据库备份与恢复流程（旧格式）"
        doc.SaveAs2(str(tmp / "旧版灾备手册.doc"), FileFormat=0)
        try:
            doc.ExportAsFixedFormat(str(tmp / "灾备手册.pdf"), 17)
        except Exception as exc:  # noqa: BLE001
            print("WARN PDF 导出失败（pypdf 分支将跳过）：", str(exc)[:100])
        doc.Close(0)
        app.Quit()
        out["doc"] = tmp / "旧版灾备手册.doc"
        if (tmp / "灾备手册.pdf").exists():
            out["pdf"] = tmp / "灾备手册.pdf"

        app = _wps_app("KWPP.Application")
        pres = app.Presentations.Add()
        slide = pres.Slides.Add(1, 1)
        slide.Shapes(1).TextFrame.TextRange.Text = MARKER + " 数据库（旧格式）"
        pres.SaveAs(str(tmp / "旧版灾备演示.ppt"), FileFormat=1)
        pres.Close()
        app.Quit()
        out["ppt"] = tmp / "旧版灾备演示.ppt"

        app = _wps_app("KET.Application")
        book = app.Workbooks.Add()
        sheet = book.Worksheets(1)
        sheet.Cells(1, 1).Value = MARKER
        sheet.Cells(2, 1).Value = "数据库"
        book.SaveAs(str(tmp / "旧版灾备清单.xls"), FileFormat=56)
        book.Close(0)
        app.Quit()
        out["xls"] = tmp / "旧版灾备清单.xls"
    finally:
        pythoncom.CoUninitialize()

    # 敏感文件：按硬约束不得进 FTS
    (tmp / "系统密码清单.txt").write_text("admin / p@ssw0rd-不应进索引", encoding="utf-8")
    return out


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def main() -> int:
    purged = purge_stale_roots()
    if purged:
        print("CLEAN 历史遗留测试目录", purged, flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="onlyone_m2_"))
    rid = None
    proc = None
    port = 0
    results: list[bool] = []
    try:
        samples = make_samples(tmp)
        print("生成样例：", ", ".join(sorted(samples)), flush=True)

        # 1) 抽取器：六类 + PDF/TXT 都必须抽出正文，且不得抛异常
        for label in ("doc", "docx", "xls", "xlsx", "ppt", "pptx", "pdf", "txt"):
            p = samples.get(label)
            if not p:
                print("SKIP extract", label, "（样例未生成）", flush=True)
                continue
            r = extract(p)
            ok = (MARKER in r.body) and not r.error
            results.append(check("extract %-5s" % label, ok,
                                 "len=%d tables=%d err=%s" % (len(r.body), len(r.tables), r.error[:80])))

        # 2) 起后端，扫描临时目录
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
        for _ in range(120):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.15)
        else:
            raise RuntimeError("后端未就绪，检查 data/onlyone.log")

        r = http(base, "/api/roots", "POST", {"path": str(tmp), "kind": "local"})
        rid = r["id"]
        scan = http(base, "/api/roots/%d/scan" % rid, "POST", {})
        print("SCAN", {k: scan[k] for k in ("scanned", "added", "extracted",
                                            "extract_failed", "seconds")}, flush=True)
        results.append(check("扫描状态", scan["extracted"] >= 8 and scan["extract_failed"] == 0,
                             "extracted=%d failed=%d" % (scan["extracted"], scan["extract_failed"])))

        conn = connect()
        try:
            rows = conn.execute("SELECT rel_path, extract_status, extract_error "
                                "FROM files WHERE root_id=? ORDER BY rel_path", (rid,)).fetchall()
            for row in rows:
                print("   %-24s %-18s %s" % (Path(row[0]).name, row[1], row[2][:60]), flush=True)
            statuses = {r[1] for r in rows}
            results.append(check("敏感文件被排除", "skipped_sensitive" in statuses))
        finally:
            conn.close()

        # 3) 两字中文 LIKE 兜底：正文命中、snippet 非空
        short = http(base, "/api/search?q=" + urllib.parse.quote(SHORT))
        print("SEARCH 短词 '%s' -> mode=%s hits=%d" % (SHORT, short["mode"], len(short["items"])),
              flush=True)
        results.append(check("短词 LIKE 兜底", short["mode"] == "like_fallback"
                             and len(short["items"]) >= 8, "hits=%d" % len(short["items"])))
        results.append(check("命中片段", all(i.get("snippet") for i in short["items"])))

        # 4) 六字词 FTS5 trigram
        long = http(base, "/api/search?q=" + urllib.parse.quote(MARKER))
        print("SEARCH 长词 '%s' -> mode=%s hits=%d" % (MARKER, long["mode"], len(long["items"])),
              flush=True)
        results.append(check("FTS5 trigram 正文命中", long["mode"] == "fts5_trigram"
                             and len(long["items"]) >= 6, "hits=%d" % len(long["items"])))

        # 5) 敏感内容完全不可检索
        secret = http(base, "/api/search?q=" + urllib.parse.quote("密码"))
        bad = [i["title"] for i in secret["items"] if "密码" in i["title"]]
        results.append(check("凭据文件不进 FTS", not bad, str(bad)))

        # 6) 只读预览：正文 + 表格
        docx_item = next((i for i in short["items"] if i["title"].endswith(".docx")), None)
        if docx_item:
            pv = http(base, "/api/preview?entry_id=%d" % docx_item["id"])
            results.append(check("只读预览正文", MARKER in pv["text"], pv["error"][:80]))
            results.append(check("只读预览表格", len(pv["tables"]) > 0,
                                 "tables=%d" % len(pv["tables"])))
        else:
            results.append(check("只读预览正文", False, "未找到 docx 条目"))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d)" % (sum(results), len(results)),
              flush=True)
        return 0 if ok else 1
    finally:
        if rid is not None and proc is not None and port:
            try:
                http("http://127.0.0.1:%d" % port, "/api/roots/%d" % rid, "DELETE")
            except Exception:  # noqa: BLE001
                pass
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        purge_stale_roots()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
