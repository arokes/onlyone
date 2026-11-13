# -*- coding: utf-8 -*-
"""记事本（M7）：笔记本 → 分区 → 页面三级结构、富文本自动保存、图片附件、导出与全库备份。

设计要点：

* ``content_json`` 存 ProseMirror/TipTap 结构；``content_text`` 是后端抽取的纯文本，
  专供 FTS / 语义索引，避免把 JSON 标签写进索引。
* ``is_private=1`` 的页面**完全不建 entries 行**，因此不进 FTS、语义向量与全局搜索。
* 自动保存带 ``revision`` 乐观锁；仅内容/标题/私密标志变化才同步索引，并 checkpoint
  减少 WAL 中的明文残留。
* 图片落盘到 ``data/attachments/<note_id>/``，通过只读 API 访问，绝不进 FTS。
"""
from __future__ import annotations

import base64
import html
import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import entries, semantic
from .db import DATA_DIR, DB_PATH, connect

log = logging.getLogger("onlyone")
router = APIRouter()

ATTACH_DIR = DATA_DIR / "attachments"
BACKUP_DIR = DATA_DIR / "backups"
MAX_INDEX_CHARS = 20_000          # 入 FTS / 语义向量的正文上限
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SAFE_FILENAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|gif|webp)$")
BLOCK_TYPES = {"paragraph", "heading", "codeBlock", "blockquote", "listItem"}


class NotebookIn(BaseModel):
    name: str
    sort_order: int = 0


class SectionIn(BaseModel):
    notebook_id: int
    name: str
    sort_order: int = 0


class NoteCreate(BaseModel):
    section_id: int
    title: str = "未命名页面"


class NoteUpdate(BaseModel):
    title: str | None = None
    content_json: str | None = None
    is_private: bool | None = None
    revision: int


class MoveIn(BaseModel):
    section_id: int | None = None
    sort_order: int | None = None


class ImageIn(BaseModel):
    data: str
    name: str = "image.png"


# ---------------------------------------------------------------- 内容转换

def _parse_doc(content_json: str) -> dict:
    if not content_json:
        return {"type": "doc", "content": []}
    try:
        doc = json.loads(content_json)
    except (ValueError, TypeError):
        return {"type": "doc", "content": []}
    return doc if isinstance(doc, dict) else {"type": "doc", "content": []}


def _extract_text(content_json: str) -> str:
    """从 ProseMirror JSON 抽纯文本（FTS / 语义 / 摘要用）。"""
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "text":
            parts.append(str(node.get("text") or ""))
        elif kind == "hardBreak":
            parts.append("\n")
        for child in node.get("content") or []:
            walk(child)
        if kind in BLOCK_TYPES:
            parts.append("\n")

    walk(_parse_doc(content_json).get("content") or [])
    return "".join(parts)


def _to_markdown(title: str, content_json: str) -> str:
    def render(node) -> str:
        kind = node.get("type")
        if kind == "text":
            text = str(node.get("text") or "")
            marks = {m.get("type") for m in node.get("marks") or []}
            if "code" in marks:
                return "`%s`" % text
            if "bold" in marks:
                text = "**%s**" % text
            if "italic" in marks:
                text = "*%s*" % text
            return text
        if kind == "hardBreak":
            return "\n"
        if kind == "image":
            attrs = node.get("attrs") or {}
            src = str(attrs.get("src") or "").split("?", 1)[0]
            src = ("attachments/" + src[len("/api/attachments/"):]
                   if src.startswith("/api/attachments/") else "")
            return "![%s](%s)" % (attrs.get("alt", ""), src)
        inner = "".join(render(c) for c in node.get("content") or [])
        if kind == "paragraph":
            return inner + "\n\n"
        if kind == "heading":
            level = int((node.get("attrs") or {}).get("level", 1))
            return "#" * level + " " + inner + "\n\n"
        if kind == "codeBlock":
            return "```\n%s\n```\n\n" % inner
        if kind == "listItem":
            return "- " + inner.strip() + "\n"
        if kind == "bulletList":
            return inner + "\n"
        return inner

    body = "".join(render(c) for c in _parse_doc(content_json).get("content") or [])
    return "# %s\n\n%s" % (title, body)


def _to_html(title: str, content_json: str) -> str:
    def render(node) -> str:
        kind = node.get("type")
        if kind == "text":
            text = html.escape(str(node.get("text") or ""))
            marks = {m.get("type") for m in node.get("marks") or []}
            if "code" in marks:
                return "<code>%s</code>" % text
            if "bold" in marks:
                text = "<strong>%s</strong>" % text
            if "italic" in marks:
                text = "<em>%s</em>" % text
            return text
        if kind == "hardBreak":
            return "<br>"
        if kind == "image":
            attrs = node.get("attrs") or {}
            src = str(attrs.get("src") or "").split("?", 1)[0]
            # 只允许本地附件路径，并重写成 zip 内相对路径；避免外链、脚本与令牌外泄
            src = ("attachments/" + src[len("/api/attachments/"):]
                   if src.startswith("/api/attachments/") else "")
            return '<img src="%s" alt="%s">' % (
                html.escape(src, quote=True), html.escape(str(attrs.get("alt") or ""), quote=True))
        inner = "".join(render(c) for c in node.get("content") or [])
        if kind == "paragraph":
            return "<p>%s</p>" % inner
        if kind == "heading":
            level = min(6, max(1, int((node.get("attrs") or {}).get("level", 1))))
            return "<h%d>%s</h%d>" % (level, inner, level)
        if kind == "bulletList":
            return "<ul>%s</ul>" % inner
        if kind == "orderedList":
            return "<ol>%s</ol>" % inner
        if kind == "listItem":
            return "<li>%s</li>" % inner
        if kind == "codeBlock":
            return "<pre><code>%s</code></pre>" % inner
        if kind == "blockquote":
            return "<blockquote>%s</blockquote>" % inner
        return inner

    body = "".join(render(c) for c in _parse_doc(content_json).get("content") or [])
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>%s</title></head><body><h1>%s</h1>%s</body></html>"
            % (html.escape(title), html.escape(title), body))


# ---------------------------------------------------------------- 统一条目同步

def _sync_note_entry(conn, row) -> None:
    """私密页不建条目；公开页用截断正文同步 FTS + 语义向量。"""
    existing = conn.execute(
        "SELECT id FROM entries WHERE type='note' AND ref_id=?", (row["id"],)
    ).fetchone()
    if row["is_private"]:
        if existing:
            conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (existing["id"],))
            semantic.drop_entry(conn, existing["id"])
            conn.execute("DELETE FROM entries WHERE id=?", (existing["id"],))
        return
    proxy = dict(row)
    proxy["content_text"] = (row["content_text"] or "")[:MAX_INDEX_CHARS]
    entries.sync_entry(conn, "notes", proxy)


def _drop_note_entry(conn, note_id: int) -> None:
    entries.drop_entry(conn, "notes", note_id)


def _purge_notes_entries(conn, note_ids: list[int]) -> None:
    for note_id in note_ids:
        _drop_note_entry(conn, note_id)


def _checkpoint(conn) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001
        log.debug("checkpoint 跳过：%s", exc)


# ---------------------------------------------------------------- 三级结构

@router.get("/api/notebooks")
def tree():
    conn = connect()
    try:
        books = [dict(r) for r in conn.execute(
            "SELECT * FROM notebooks ORDER BY sort_order, id")]
        for book in books:
            sections = [dict(r) for r in conn.execute(
                "SELECT * FROM sections WHERE notebook_id=? ORDER BY sort_order, id",
                (book["id"],))]
            for section in sections:
                section["notes"] = [dict(r) for r in conn.execute(
                    """SELECT id, section_id, title, sort_order, is_private, revision,
                              updated_at, substr(content_text,1,120) AS excerpt
                       FROM notes WHERE section_id=? ORDER BY sort_order, id""",
                    (section["id"],))]
            book["sections"] = sections
        return books
    finally:
        conn.close()


@router.post("/api/notebooks")
def create_notebook(body: NotebookIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "笔记本名称不能为空")
    conn = connect()
    try:
        nid = conn.execute(
            "INSERT INTO notebooks(name,sort_order) VALUES(?,?)",
            (name, body.sort_order)).lastrowid
        conn.commit()
        return {"id": nid, "name": name}
    finally:
        conn.close()


@router.put("/api/notebooks/{notebook_id}")
def rename_notebook(notebook_id: int, body: NotebookIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "笔记本名称不能为空")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone():
            raise HTTPException(404, "笔记本不存在")
        conn.execute("UPDATE notebooks SET name=?, sort_order=?, "
                     "updated_at=datetime('now','localtime') WHERE id=?",
                     (name, body.sort_order, notebook_id))
        conn.commit()
        return {"id": notebook_id}
    finally:
        conn.close()


@router.delete("/api/notebooks/{notebook_id}")
def delete_notebook(notebook_id: int):
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone():
            raise HTTPException(404, "笔记本不存在")
        ids = [r[0] for r in conn.execute(
            """SELECT n.id FROM notes n JOIN sections s ON n.section_id=s.id
               WHERE s.notebook_id=?""", (notebook_id,))]
        _purge_notes_entries(conn, ids)
        conn.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))
        conn.commit()
        # 附件目录随笔记本一起清理
        for note_id in ids:
            shutil.rmtree(ATTACH_DIR / str(note_id), ignore_errors=True)
        return {"deleted": notebook_id, "notes": len(ids)}
    finally:
        conn.close()


@router.post("/api/sections")
def create_section(body: SectionIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "分区名称不能为空")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM notebooks WHERE id=?",
                            (body.notebook_id,)).fetchone():
            raise HTTPException(404, "笔记本不存在")
        sid = conn.execute(
            "INSERT INTO sections(notebook_id,name,sort_order) VALUES(?,?,?)",
            (body.notebook_id, name, body.sort_order)).lastrowid
        conn.commit()
        return {"id": sid, "name": name}
    finally:
        conn.close()


@router.put("/api/sections/{section_id}")
def rename_section(section_id: int, body: SectionIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "分区名称不能为空")
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM sections WHERE id=?", (section_id,)).fetchone():
            raise HTTPException(404, "分区不存在")
        conn.execute("UPDATE sections SET name=?, sort_order=?, notebook_id=?, "
                     "updated_at=datetime('now','localtime') WHERE id=?",
                     (name, body.sort_order, body.notebook_id, section_id))
        conn.commit()
        return {"id": section_id}
    finally:
        conn.close()


@router.delete("/api/sections/{section_id}")
def delete_section(section_id: int):
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM sections WHERE id=?", (section_id,)).fetchone():
            raise HTTPException(404, "分区不存在")
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM notes WHERE section_id=?", (section_id,))]
        _purge_notes_entries(conn, ids)
        conn.execute("DELETE FROM sections WHERE id=?", (section_id,))
        conn.commit()
        for note_id in ids:
            shutil.rmtree(ATTACH_DIR / str(note_id), ignore_errors=True)
        return {"deleted": section_id, "notes": len(ids)}
    finally:
        conn.close()


# ---------------------------------------------------------------- 导出（放在 /{note_id} 之前）

@router.get("/api/notes/export")
def export_notes(scope: str = "page", id: int = 0, format: str = "md"):
    """导出单页 / 分区 / 笔记本。format=md | html | zip（html + 图片打包）。"""
    if scope not in ("page", "section", "notebook") or format not in ("md", "html", "zip"):
        raise HTTPException(400, "scope / format 不合法")
    conn = connect()
    try:
        if scope == "page":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE id=?", (id,))]
            # HTTP 头只能 latin-1，文件名必须用 ASCII；中文标题在导出内容里保留
            label = "note-%d" % id
        elif scope == "section":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE section_id=? ORDER BY sort_order,id", (id,))]
            label = "section-%s" % id
        else:
            rows = [dict(r) for r in conn.execute(
                """SELECT n.* FROM notes n JOIN sections s ON n.section_id=s.id
                   WHERE s.notebook_id=? ORDER BY s.sort_order, n.sort_order, n.id""", (id,))]
            label = "notebook-%s" % id
    finally:
        conn.close()
    if not rows:
        raise HTTPException(404, "该范围没有页面")

    if format == "md":
        text = "\n\n---\n\n".join(
            _to_markdown(r["title"], r["content_json"]) for r in rows)
        return Response(
            text, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="%s.md"' % label})

    html_doc = "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">" \
               "<title>%s</title></head><body>%s</body></html>" % (
                   html.escape(label),
                   "".join(_to_html(r["title"], r["content_json"]) for r in rows))
    if format == "html":
        return Response(html_doc, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s.html"' % label})

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html_doc)
        for row in rows:
            folder = ATTACH_DIR / str(row["id"])
            if not folder.is_dir():
                continue
            for image in folder.iterdir():
                if image.is_file():
                    archive.write(str(image), "attachments/%s/%s" % (row["id"], image.name))
    buffer.seek(0)
    return Response(
        buffer.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="%s.zip"' % label})


@router.post("/api/backup")
def backup_database():
    """全库备份：checkpoint 后复制 db（含凭据密文与 FTS 明文，需自行妥善保管）。"""
    conn = connect()
    try:
        _checkpoint(conn)
    finally:
        conn.close()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = "onlyone-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / name
    shutil.copy2(str(DB_PATH), str(target))
    log.info("全库备份完成 %s", name)
    return {"file": name, "path": str(target), "size": target.stat().st_size}


# ---------------------------------------------------------------- 页面

@router.post("/api/notes")
def create_note(body: NoteCreate):
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM sections WHERE id=?",
                            (body.section_id,)).fetchone():
            raise HTTPException(404, "分区不存在")
        nid = conn.execute(
            "INSERT INTO notes(section_id,title) VALUES(?,?)",
            (body.section_id, body.title.strip() or "未命名页面")).lastrowid
        row = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        _sync_note_entry(conn, row)
        conn.commit()
        return {"id": nid, "revision": 0}
    finally:
        conn.close()


@router.get("/api/notes/{note_id}")
def get_note(note_id: int):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "页面不存在")
        return dict(row)
    finally:
        conn.close()


@router.put("/api/notes/{note_id}")
def update_note(note_id: int, body: NoteUpdate):
    """自动保存：revision 乐观锁；仅内容/标题/私密变化才同步索引。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "页面不存在")
        if body.revision != row["revision"]:
            raise HTTPException(409, "页面已被其他编辑更新（revision 冲突）")
        changed = False
        sets: list[str] = []
        params: list = []
        if body.title is not None:
            title = body.title.strip() or "未命名页面"
            if title != row["title"]:
                changed = True
            sets.append("title=?")
            params.append(title)
        if body.content_json is not None:
            text = _extract_text(body.content_json)
            if body.content_json != row["content_json"]:
                changed = True
            sets += ["content_json=?", "content_text=?"]
            params += [body.content_json, text]
        if body.is_private is not None:
            flag = 1 if body.is_private else 0
            if flag != row["is_private"]:
                changed = True
            sets.append("is_private=?")
            params.append(flag)
        sets += ["revision=revision+1", "updated_at=datetime('now','localtime')"]
        conn.execute("UPDATE notes SET " + ", ".join(sets) + " WHERE id=?",
                     [*params, note_id])
        new_row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if changed:
            _sync_note_entry(conn, new_row)
        conn.commit()
        _checkpoint(conn)
        return {"id": note_id, "revision": new_row["revision"],
                "saved_at": new_row["updated_at"], "changed": changed}
    finally:
        conn.close()


@router.post("/api/notes/{note_id}/move")
def move_note(note_id: int, body: MoveIn):
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone():
            raise HTTPException(404, "页面不存在")
        sets, params = [], []
        if body.section_id is not None:
            if not conn.execute("SELECT 1 FROM sections WHERE id=?",
                                (body.section_id,)).fetchone():
                raise HTTPException(404, "目标分区不存在")
            sets.append("section_id=?")
            params.append(body.section_id)
        if body.sort_order is not None:
            sets.append("sort_order=?")
            params.append(body.sort_order)
        if sets:
            conn.execute("UPDATE notes SET " + ", ".join(sets) + " WHERE id=?",
                         [*params, note_id])
            conn.commit()
        return {"id": note_id}
    finally:
        conn.close()


@router.delete("/api/notes/{note_id}")
def delete_note(note_id: int):
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone():
            raise HTTPException(404, "页面不存在")
        _drop_note_entry(conn, note_id)
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        conn.commit()
        shutil.rmtree(ATTACH_DIR / str(note_id), ignore_errors=True)
        return {"deleted": note_id}
    finally:
        conn.close()


# ---------------------------------------------------------------- 图片附件

def _image_ext(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


@router.post("/api/notes/{note_id}/images")
def upload_image(note_id: int, body: ImageIn):
    """base64 上传（无需 python-multipart）；校验魔数，避免伪装脚本文件。"""
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM notes WHERE id=?", (note_id,)).fetchone():
            raise HTTPException(404, "页面不存在")
    finally:
        conn.close()
    if not body.data.startswith("data:image/"):
        raise HTTPException(400, "只接受 data:image/ 开头的图片")
    try:
        _, encoded = body.data.split(",", 1)
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "图片数据不是合法 base64") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "图片为空或超过 10 MB")
    ext = _image_ext(raw)
    if ext is None:
        raise HTTPException(400, "不支持的图片格式（仅 png/jpg/gif/webp）")
    filename = "%s.%s" % (uuid.uuid4().hex, ext)
    folder = ATTACH_DIR / str(note_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(raw)
    return {"url": "/api/attachments/%d/%s" % (note_id, filename),
            "filename": filename, "size": len(raw)}


@router.get("/api/attachments/{note_id}/{filename}")
def get_attachment(note_id: int, filename: str):
    if not SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(400, "非法文件名")
    path = ATTACH_DIR / str(note_id) / filename
    if not path.is_file():
        raise HTTPException(404, "附件不存在")
    return FileResponse(str(path))
