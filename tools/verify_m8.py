# -*- coding: utf-8 -*-
"""M8 验收集：后台扫描 + 进度上报 + 分块提交（写锁不再被长扫描占满）。

覆盖三类回归：

1. **进度**：``POST /api/roots/{id}/scan?wait=false`` 立即返回，``/api/scan/status``
   能读到 total / done / percent，最终 phase=done。
2. **并发写**：扫描进行中连续创建项目 / 凭据，全部成功（修复前会 database is locked 报 500）。
3. **抽取与排除**：带表格的 pptx 能抽出正文（python-pptx 切片缺陷）；
   ``$RECYCLE.BIN`` / ``Windows`` 这类系统目录不进入索引。

用法：py -3 tools\\verify_m8.py
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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
from onlyone.db import connect  # noqa: E402

TOKEN = "m8-verify-token-20261106"
PREFIX = "onlyone_m8_"
PROJECT_PREFIX = "M8并发项目-"
passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  <- " + detail) if detail else ""))


def http(base: str, path: str, method: str = "GET", body=None, timeout: float = 60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_status(base: str, path: str, method: str = "GET", body=None) -> int:
    try:
        http(base, path, method, body, timeout=10)
        return 200
    except urllib.error.HTTPError as exc:
        return exc.code


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def purge() -> None:
    """清理历史 M8 数据（根目录 / 项目 / 凭据），保证可反复运行。"""
    conn = connect()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM roots WHERE path LIKE ?", ("%" + PREFIX + "%",))]
        for rid in ids:
            for f in conn.execute("SELECT id FROM files WHERE root_id=?", (rid,)).fetchall():
                conn.execute("DELETE FROM fts_entries WHERE entry_id IN "
                             "(SELECT id FROM entries WHERE type='file' AND ref_id=?)", (f[0],))
                conn.execute("DELETE FROM entries WHERE type='file' AND ref_id=?", (f[0],))
            conn.execute("DELETE FROM extract_failures WHERE file_id IN "
                         "(SELECT id FROM files WHERE root_id=?)", (rid,))
            conn.execute("DELETE FROM files WHERE root_id=?", (rid,))
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        for pid in [r[0] for r in conn.execute(
                "SELECT id FROM projects WHERE name LIKE ?", (PROJECT_PREFIX + "%",))]:
            for eid in [r[0] for r in conn.execute(
                    "SELECT id FROM entries WHERE type='project' AND ref_id=?", (pid,))]:
                conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (eid,))
                conn.execute("DELETE FROM semantic_index WHERE entry_id=?", (eid,))
                conn.execute("DELETE FROM entries WHERE id=?", (eid,))
            conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.execute("DELETE FROM credentials WHERE title LIKE ?", ("M8凭据-%",))
        conn.commit()
    finally:
        conn.close()


def build_tree(base: Path) -> None:
    """造一个含正常文档 + 系统目录的小目录树，用于校验抽取与剪枝。"""
    (base / "笔记").mkdir(parents=True, exist_ok=True)
    (base / "笔记" / "说明.txt").write_text(
        "OnlyOne M8 后台扫描进度验证 正文中文检索", encoding="utf-8")

    from docx import Document

    doc = Document()
    doc.add_paragraph("M8 抽取验证 docx 正文")
    doc.save(str(base / "笔记" / "交付说明.docx"))

    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "M8 表格页"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1.5)).table
    table.cell(0, 0).text = "模块"
    table.cell(0, 1).text = "状态"
    table.cell(1, 0).text = "库存结转"
    table.cell(1, 1).text = "已完成"
    prs.save(str(base / "表格演示.pptx"))

    # 系统目录：必须被 SKIP_DIRS 剪枝，不产生任何 files 行。
    (base / "$RECYCLE.BIN" / "S-1-5-21").mkdir(parents=True, exist_ok=True)
    (base / "$RECYCLE.BIN" / "S-1-5-21" / "回收站残留.txt").write_text("不应被索引", encoding="utf-8")
    (base / "Windows" / "System32").mkdir(parents=True, exist_ok=True)
    (base / "Windows" / "System32" / "系统文件.txt").write_text("不应被索引", encoding="utf-8")

    # 撑出几秒扫描时长，让进度条有可观测的中间态。
    bulk = base / "批量"
    bulk.mkdir(exist_ok=True)
    for i in range(400):
        (bulk / f"日志{i:03d}.txt").write_text(
            "批量文件 %d 用于延长扫描时间，验证进度上报与并发写不冲突。" % i, encoding="utf-8")


def main() -> int:
    purge()
    tmp = Path(tempfile.mkdtemp(prefix=PREFIX))
    build_tree(tmp)

    port = free_port()
    env = dict(os.environ, ONLYONE_TOKEN=TOKEN)
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    base = "http://127.0.0.1:%d" % port
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                http(base, "/api/meta/status", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            print("后端启动超时")
            return 1

        print("== M8-1 后台扫描与进度 ==")
        root = http(base, "/api/roots", "POST", {"path": str(tmp), "kind": "local"})
        rid = root["id"]

        started = http(base, "/api/roots/%d/scan?wait=false" % rid, "POST", {})
        check("提交后台扫描立即返回", started.get("started") is True, str(started))
        dup = http_status(base, "/api/roots/%d/scan?wait=false" % rid, "POST", {})
        check("扫描进行中重复提交返回 409", dup == 409, str(dup))

        # 扫描进行中连发写请求：修复前会 database is locked -> 500
        # ① 先连续采样进度：不掺写请求，避免采样窗口被锁等待吃掉
        samples: list[tuple[str, int, int]] = []
        last_st: dict = {}
        for _ in range(60):
            last_st = http(base, "/api/scan/status")
            samples.append((last_st["phase"], last_st["done"], last_st["total"]))
            if last_st["phase"] in ("done", "error"):
                break
            time.sleep(0.1)
        scanning = [s for s in samples if s[0] in ("scanning", "queued")]
        mids = [s[1] for s in samples if s[0] == "scanning" and 0 < s[1] < s[2]]
        check("扫描期间进度接口处于进行中", bool(scanning),
              " ".join("%s:%s/%s" % s for s in samples[:6]))
        check("进度 done 单调递增且有中间值", bool(mids) and mids == sorted(mids),
              str(mids[:8]))
        check("扫描最终完成", last_st.get("phase") == "done", str(last_st.get("phase")))

        stats = last_st.get("stats") or {}
        check("进度 done 与 total 一致", last_st.get("done") == last_st.get("total") > 0,
              "%s/%s" % (last_st.get("done"), last_st.get("total")))
        check("百分比为 100", last_st.get("percent") == 100.0, str(last_st.get("percent")))
        check("统计新增文件数 = 403（400 批量 + 2 文档 + 1 pptx）",
              stats.get("added") == 403, str(stats))

        # ② 再单独验证一次扫描进行中的写请求：必须成功，且不能被锁拖到超时
        #    用 rebuild 强制重新抽取，保证这段窗口真的有几秒的扫描在跑
        http(base, "/api/roots/%d/rebuild?wait=false" % rid, "POST", {})
        time.sleep(0.3)
        write_codes: list[int] = []
        write_seconds: list[float] = []
        for i in range(8):
            t0 = time.time()
            write_codes.append(http_status(
                base, "/api/biz/projects", "POST",
                {"data": {"name": "%s%d" % (PROJECT_PREFIX, i), "status": "active"}}))
            write_seconds.append(round(time.time() - t0, 2))
            time.sleep(0.1)

        check("扫描期间并发创建项目全部成功", all(c == 200 for c in write_codes),
              str(write_codes))
        check("写请求未被锁拖死（单次 < 10s）", max(write_seconds) < 10,
              str(write_seconds))

        # 等 rebuild 收尾，避免清理阶段与后台扫描抢锁
        deadline = time.time() + 120
        while time.time() < deadline:
            st = http(base, "/api/scan/status")
            if st["phase"] in ("done", "error"):
                break
            time.sleep(0.3)

        print("== M8-2 抽取（pptx 表格切片修复） ==")
        conn = connect()
        try:
            pptx_row = conn.execute(
                "SELECT extract_status, extract_error FROM files WHERE root_id=? AND rel_path LIKE ?",
                (rid, "%表格演示.pptx")).fetchone()
            docx_row = conn.execute(
                "SELECT extract_status, extract_error FROM files WHERE root_id=? AND rel_path LIKE ?",
                (rid, "%交付说明.docx")).fetchone()
            paths = [r[0] for r in conn.execute(
                "SELECT rel_path FROM files WHERE root_id=?", (rid,))]
        finally:
            conn.close()
        check("pptx 抽取成功（不再 TypeError）",
              pptx_row is not None and pptx_row["extract_status"] == "ok",
              str(dict(pptx_row)) if pptx_row else "missing")
        check("docx 抽取成功", docx_row is not None and docx_row["extract_status"] == "ok",
              str(dict(docx_row)) if docx_row else "missing")
        check("$RECYCLE.BIN 未被索引", not any("$RECYCLE.BIN" in p for p in paths))
        check("Windows 目录未被索引", not any(p.startswith("Windows") for p in paths))

        print("== M8-3 表内容进入检索 ==")
        hit = http(base, "/api/search?q=" + urllib.parse.quote("库存结转"))
        items = hit.get("items") or []
        check("pptx 表格文字可被检索到",
              any("表格演示" in str(it.get("title", "")) for it in items),
              "hits=%d" % len(items))

        print("== M8-4 凭据账号常显 ==")
        http(base, "/api/credentials", "POST", {
            "title": "M8凭据-账号明文", "username": "demo_user", "password": "s3cret"})
        creds = http(base, "/api/credentials")
        mine = [c for c in creds if c["title"] == "M8凭据-账号明文"]
        check("凭据列表返回账号明文",
              bool(mine) and mine[0]["username"] == "demo_user",
              str(mine[0]["username"]) if mine else "missing")
        check("凭据密码仍为掩码",
              bool(mine) and mine[0]["password"] == "••••••••",
              str(mine[0]["password"]) if mine else "missing")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        purge()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n结果：PASS %d / FAIL %d" % (len(passed), len(failed)))
    if failed:
        print("失败项：", "、".join(failed))
        return 1
    print("M8 ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
