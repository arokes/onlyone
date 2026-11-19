# -*- coding: utf-8 -*-
"""M7 验收集：三级记事本、富文本自动保存、私密页、图片、导出/备份、凭据加密导出恢复。

用法：py -3 tools\\verify_notes.py
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# M8 修复：本套回归用独立数据目录。凭据导出是全库范围、导入是 merge 模式，
# 直接在真实库上反复跑会把用户已有凭据一次次复制（实测 3 条被复制成 21 条）。
# 必须在 import onlyone.db 之前设置，模块导入时才计算 DATA_DIR。
DATA_DIR = Path(tempfile.mkdtemp(prefix="onlyone_m7_"))
os.environ["ONLYONE_DATA_DIR"] = str(DATA_DIR)

sys.path.insert(0, str(BACKEND))
from onlyone.db import DATA_DIR, connect  # noqa: E402

TOKEN = "m7-notes-token-20261106"
PREFIX = "M7验证-"
SECRET = "M7-p@ssw0rd-测试"
PASSPHRASE = "export-pass-2026"
PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="


def http(base: str, path: str, method: str = "GET", body=None, token: str = TOKEN):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def raw_get(base: str, path: str, token: str = TOKEN):
    req = urllib.request.Request(base + path,
                                 headers={"X-OnlyOne-Token": token})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read(), resp.headers


def expect_status(base: str, path: str, method: str, body, expected: int) -> bool:
    try:
        http(base, path, method, body)
        return False
    except urllib.error.HTTPError as exc:
        return exc.code == expected


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def purge() -> None:
    """清掉历史演示数据；笔记的 entries/fts/语义向量没有外键，必须手动清。"""
    conn = connect()
    try:
        book_ids = [r[0] for r in conn.execute(
            "SELECT id FROM notebooks WHERE name LIKE ?", (PREFIX + "%",))]
        note_ids: list[int] = []
        for bid in book_ids:
            note_ids.extend(r[0] for r in conn.execute(
                """SELECT n.id FROM notes n JOIN sections s ON n.section_id=s.id
                   WHERE s.notebook_id=?""", (bid,)))
        for nid in note_ids:
            row = conn.execute(
                "SELECT id FROM entries WHERE type='note' AND ref_id=?", (nid,)).fetchone()
            if row:
                conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (row[0],))
                conn.execute("DELETE FROM semantic_index WHERE entry_id=?", (row[0],))
                conn.execute("DELETE FROM entries WHERE id=?", (row[0],))
        conn.execute("DELETE FROM notebooks WHERE name LIKE ?", (PREFIX + "%",))
        conn.execute("DELETE FROM credentials WHERE title LIKE ?", (PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    results: list[bool] = []
    credential_ids: list[int] = []
    backup_file: Path | None = None
    try:
        for _ in range(160):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")
        purge()

        # 1) 三级结构
        book = http(base, "/api/notebooks", "POST", {"name": PREFIX + "工作笔记"})
        section = http(base, "/api/sections", "POST",
                       {"notebook_id": book["id"], "name": "运维分区"})
        note = http(base, "/api/notes", "POST",
                    {"section_id": section["id"], "title": PREFIX + "灾备预案"})
        tree = http(base, "/api/notebooks")
        found = any(b["id"] == book["id"] and any(
            s["id"] == section["id"] and any(n["id"] == note["id"] for n in s["notes"])
            for s in b["sections"]) for b in tree)
        results.append(check("三级结构创建与树查询", found))

        # 2) 富文本自动保存 + revision 乐观锁
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "灾备恢复手册 数据库备份流程 2026"}]},
            {"type": "heading", "attrs": {"level": 2}, "content": [
                {"type": "text", "text": "演练步骤"}]},
        ]}
        saved = http(base, f"/api/notes/{note['id']}", "PUT",
                     {"title": PREFIX + "灾备预案", "content_json": json.dumps(doc, ensure_ascii=False),
                      "is_private": False, "revision": 0})
        results.append(check("自动保存 revision 递增", saved["revision"] == 1))
        results.append(check("旧 revision 冲突返回 409", expect_status(
            base, f"/api/notes/{note['id']}", "PUT",
            {"content_json": json.dumps(doc), "revision": 0}, 409)))
        detail = http(base, f"/api/notes/{note['id']}")
        results.append(check("content_text 抽取纯文本",
                             "灾备恢复手册" in detail["content_text"]
                             and '"type"' not in detail["content_text"]))

        # 3) 公开页参与全局检索
        res = http(base, "/api/search?q=" + urllib.parse.quote("灾备恢复手册"))
        results.append(check("笔记正文进入 FTS",
                             any(i["type"] == "note" and i["ref_id"] == note["id"]
                                 for i in res["items"])))
        results.append(check("通用 CRUD 无法绕过私密逻辑", expect_status(
            base, "/api/biz/notes", "GET", None, 404)))

        # 4) 私密页不进索引
        private = http(base, "/api/notes", "POST",
                       {"section_id": section["id"], "title": PREFIX + "私密页"})
        http(base, f"/api/notes/{private['id']}", "PUT", {
            "title": PREFIX + "私密页",
            "content_json": json.dumps({"type": "doc", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "私密标记XYZ 不应被检索"}]}]}, ensure_ascii=False),
            "is_private": True, "revision": 0})
        secret = http(base, "/api/search?q=" + urllib.parse.quote("私密标记XYZ"))
        results.append(check("私密页不进 FTS", not secret["items"]))
        # 切回公开后应能被检索
        http(base, f"/api/notes/{private['id']}", "PUT",
             {"is_private": False, "revision": 1})
        released = http(base, "/api/search?q=" + urllib.parse.quote("私密标记XYZ"))
        results.append(check("取消私密后恢复检索", bool(released["items"])))

        # 5) 图片上传与只读访问
        image = http(base, f"/api/notes/{note['id']}/images", "POST",
                     {"data": "data:image/png;base64," + PNG_1PX, "name": "px.png"})
        status, payload, _ = raw_get(base, image["url"])
        results.append(check("图片上传与只读访问",
                             status == 200 and payload.startswith(b"\x89PNG")))
        results.append(check("伪装图片被拒绝", expect_status(
            base, f"/api/notes/{note['id']}/images", "POST",
            {"data": "data:image/png;base64," + base64.b64encode(b"not an image").decode()}, 400)))
        results.append(check("附件路径穿越被拒绝", expect_status(
            base, "/api/attachments/%d/..%%2F..%%2Fonlyone.db" % note["id"], "GET", None, 400)
            or expect_status(base, "/api/attachments/%d/not-a-file.png" % note["id"],
                             "GET", None, 400)))

        # 5b) 内容哈希命名 + 页内去重（2026-11-16 粘贴图片增强）
        raw_png = base64.b64decode(PNG_1PX)
        digest = hashlib.sha256(raw_png).hexdigest()[:32]
        image_again = http(base, f"/api/notes/{note['id']}/images", "POST",
                           {"data": "data:image/png;base64," + PNG_1PX, "name": "px.png"})
        results.append(check("同图重复上传命中去重",
                             image_again["url"] == image["url"]
                             and image["dedup"] is False and image_again["dedup"] is True))
        results.append(check("文件名为 SHA-256 前 32 位且符合白名单",
                             image["filename"] == digest + ".png"
                             and re.fullmatch(r"[0-9a-f]{32}\.png", image["filename"]) is not None))
        folder = DATA_DIR / "attachments" / str(note["id"])
        files_before = sorted(p.name for p in folder.iterdir() if p.is_file())
        results.append(check("重复上传不新增附件文件",
                             files_before == [digest + ".png"], str(files_before)))
        other_png = base64.b64encode(raw_png + b"x").decode()
        image_other = http(base, f"/api/notes/{note['id']}/images", "POST",
                           {"data": "data:image/png;base64," + other_png, "name": "px2.png"})
        results.append(check("不同字节不误判为重复",
                             image_other["url"] != image["url"]
                             and image_other["dedup"] is False))

        # 5c) 表格：合并单元格导出 + 单元格文字进索引（2026-11-18）
        current = http(base, f"/api/notes/{note['id']}")

        def _cell(text: str, cs: int = 1, rs: int = 1, header: bool = False) -> dict:
            para: dict = {"type": "paragraph"}
            if text:
                para["content"] = [{"type": "text", "text": text}]
            return {"type": "tableHeader" if header else "tableCell",
                    "attrs": {"colspan": cs, "rowspan": rs, "colwidth": None},
                    "content": [para]}

        table_doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "灾备恢复手册 数据库备份流程 2026"}]},
            {"type": "table", "content": [
                {"type": "tableRow", "content": [
                    _cell("单元", header=True), _cell("状态", header=True),
                    _cell("备注", header=True)]},
                {"type": "tableRow", "content": [
                    _cell("主库", rs=2), _cell("已演练"), _cell("含|管道")]},
                {"type": "tableRow", "content": [_cell("回滚中"), _cell("")]},
            ]},
        ]}
        http(base, f"/api/notes/{note['id']}", "PUT",
             {"content_json": json.dumps(table_doc, ensure_ascii=False),
              "revision": current["revision"]})
        st, md, _ = raw_get(base, f"/api/notes/export?scope=page&id={note['id']}&format=md")
        md_text = md.decode("utf-8")
        results.append(check("表格导出 Markdown（GFM + 管道转义 + 合并拍平）",
                             st == 200
                             and "| 单元 | 状态 | 备注 |" in md_text
                             and "| --- | --- | --- |" in md_text
                             and "含\\|管道" in md_text
                             and "|  | 回滚中 |  |" in md_text, md_text.strip()[-120:]))
        st, html_bytes, _ = raw_get(
            base, f"/api/notes/export?scope=page&id={note['id']}&format=html")
        html_text = html_bytes.decode("utf-8")
        results.append(check("表格导出 HTML（thead + rowspan + 内联样式）",
                             st == 200 and "<thead>" in html_text
                             and 'rowspan="2"' in html_text
                             and "border-collapse" in html_text))
        hits = http(base, "/api/search?q=" + urllib.parse.quote("回滚中"))
        results.append(check("表格单元格文字进入 FTS",
                             any(i["type"] == "note" and i["ref_id"] == note["id"]
                                 for i in hits["items"])))

        # 6) 导出 md / zip
        st, md, _ = raw_get(base, f"/api/notes/export?scope=page&id={note['id']}&format=md")
        results.append(check("Markdown 导出",
                             st == 200 and "灾备恢复手册" in md.decode("utf-8")))
        st, zipped, _ = raw_get(
            base, f"/api/notes/export?scope=notebook&id={book['id']}&format=zip")
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            names = archive.namelist()
        results.append(check("HTML+图片 zip 导出",
                             st == 200 and "index.html" in names
                             and any(n.startswith("attachments/") for n in names),
                             str(names[:4])))

        # 7) 全库备份
        backup = http(base, "/api/backup", "POST", {})
        backup_file = Path(backup["path"])
        results.append(check("全库备份落盘", backup_file.exists()
                             and backup_file.stat().st_size > 10000))

        # 8) 凭据 C 方案：加密导出 / 错误口令 / 跨机恢复
        cred = http(base, "/api/credentials", "POST", {
            "title": PREFIX + "导出测试", "username": "m7_admin",
            "password": SECRET, "url": "https://example.com", "note": "M7"})
        credential_ids.append(cred["id"])
        exported = http(base, "/api/credentials/export", "POST",
                        {"passphrase": PASSPHRASE})
        results.append(check("导出载荷只含密文",
                             exported["format"] == "onlyone-credentials-v1"
                             and SECRET not in json.dumps(exported, ensure_ascii=False)))
        results.append(check("错误口令被拒绝", expect_status(
            base, "/api/credentials/import", "POST",
            {"passphrase": "wrong-passphrase", "payload": exported, "mode": "merge"}, 400)))
        http(base, f"/api/credentials/{cred['id']}", "DELETE", None)
        restored = http(base, "/api/credentials/import", "POST",
                        {"passphrase": PASSPHRASE, "payload": exported, "mode": "merge"})
        listing = http(base, "/api/credentials")
        restored_row = next((r for r in listing if str(r["title"]).startswith(PREFIX)), None)
        revealed = ""
        if restored_row:
            revealed = http(base, f"/api/credentials/{restored_row['id']}/reveal?field=password")["value"]
            credential_ids.append(restored_row["id"])
        results.append(check("加密导出可跨账户恢复",
                             restored["imported"] == 1 and revealed == SECRET,
                             "revealed=%s" % ("OK" if revealed == SECRET else "mismatch")))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d)" % (sum(results), len(results)),
              flush=True)
        return 0 if ok else 1
    finally:
        for cid in credential_ids:
            try:
                http(base, f"/api/credentials/{cid}", "DELETE", None)
            except Exception:  # noqa: BLE001
                pass
        if backup_file is not None:
            try:
                backup_file.unlink()
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        purge()
        shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
