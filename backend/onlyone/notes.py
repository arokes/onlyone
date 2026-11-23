# -*- coding: utf-8 -*-
"""记事本（M7）：笔记本 → 分区 → 页面三级结构、富文本自动保存、图片附件、导出与全库备份。

设计要点：

* ``content_json`` 存 ProseMirror/TipTap 结构；``content_text`` 是后端抽取的纯文本，
  专供 FTS / 语义索引，避免把 JSON 标签写进索引。
* ``is_private=1`` 的页面**完全不建 entries 行**，因此不进 FTS、语义向量与全局搜索。
* 自动保存带 ``revision`` 乐观锁；仅内容/标题/私密标志变化才同步索引，并 checkpoint
  减少 WAL 中的明文残留。
* 图片落盘到 ``data/attachments/<note_id>/``，文件名取内容 SHA-256 前 32 位，
  同页内重复图片自动去重；通过只读 API 访问，绝不进 FTS。
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import desktop, entries, mht_import, semantic
from .db import DATA_DIR, DB_PATH, connect

log = logging.getLogger("onlyone")
router = APIRouter()

ATTACH_DIR = DATA_DIR / "attachments"
BACKUP_DIR = DATA_DIR / "backups"
MAX_INDEX_CHARS = 20_000          # 入 FTS / 语义向量的正文上限
MAX_IMAGE_BYTES = 10 * 1024 * 1024
PROJECT_BOOK = "项目笔记"        # 项目工作台新建页面时的默认笔记本
SAFE_FILENAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|gif|webp)$")
BLOCK_TYPES = {"paragraph", "heading", "codeBlock", "blockquote", "listItem"}
# 导出 HTML 是脱离应用的独立文件，无法引用前端 CSS 变量，这里内联等价的暖纸色值；
# 只覆盖表格与基础排版，不做像素级复刻。
EXPORT_STYLE = (
    "body{font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
    "line-height:1.75;color:#26231e;background:#fffdf9;max-width:900px;"
    "margin:32px auto;padding:0 24px}"
    "table{border-collapse:collapse;width:100%;margin:14px 0;table-layout:fixed}"
    "th,td{border:1px solid #e5dbcd;padding:6px 10px;text-align:left;"
    "vertical-align:top;word-break:break-word;font-size:14px}"
    "th{background:#f1e9dc;font-weight:600}"
    "img{max-width:100%}"
)


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
    project_id: int | None = None


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


class MhtFileIn(BaseModel):
    name: str
    data: str                  # base64（与图片上传一致，避免引入 python-multipart）


class MhtImportIn(BaseModel):
    notebook_id: int | None = None
    book_name: str = "SAP 笔记"
    files: list[MhtFileIn]


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


def _int_attr(attrs: dict, name: str) -> int:
    try:
        return max(1, int((attrs or {}).get(name) or 1))
    except (TypeError, ValueError):
        return 1


def _table_grid(table: dict, cell_text: Callable[[dict], str]) -> list[list[str]]:
    """把 ProseMirror table 节点摊成等宽的二维文本网格。

    GFM 表格没有合并语义：``colspan`` 展开成空列、``rowspan`` 在后续行补空位，
    保证每行列数一致，否则任何 Markdown 渲染器都会错位。HTML 导出不走这里，
    由 ``_render_html_table`` 原样保留 colspan / rowspan。
    """
    rows = [r for r in (table.get("content") or []) if isinstance(r, dict)]
    grid: list[list[str]] = []
    taken: list[set[int]] = []          # taken[i] = 第 i 行被上方 rowspan 占住的列号
    for index, row in enumerate(rows):
        while len(taken) <= index:
            taken.append(set())
        line: list[str] = []
        col = 0
        for cell in row.get("content") or []:
            if not isinstance(cell, dict):
                continue
            while col in taken[index]:
                line.append("")
                col += 1
            attrs = cell.get("attrs") or {}
            colspan = _int_attr(attrs, "colspan")
            rowspan = _int_attr(attrs, "rowspan")
            line.append(cell_text(cell))
            line.extend([""] * (colspan - 1))
            for span_row in range(index + 1, index + rowspan):
                while len(taken) <= span_row:
                    taken.append(set())
                taken[span_row].update(range(col, col + colspan))
            col += colspan
        while col in taken[index]:       # 行尾仍被上方 rowspan 占住
            line.append("")
            col += 1
        grid.append(line)
    width = max((len(line) for line in grid), default=0)
    for line in grid:
        line.extend([""] * (width - len(line)))
    return grid


def _render_md_table(table: dict, cell_text: Callable[[dict], str]) -> str:
    """输出 GFM 表格；首行固定当表头（Markdown 无法表达无表头表格）。"""
    grid = _table_grid(table, cell_text)
    if not grid or not grid[0]:
        return ""
    width = len(grid[0])
    lines = ["| " + " | ".join(grid[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in grid[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n\n"


def _render_html_table(table: dict, cell_html: Callable[[dict], str]) -> str:
    rows = [r for r in (table.get("content") or []) if isinstance(r, dict)]
    if not rows:
        return ""

    def row_html(row) -> str:
        cells: list[str] = []
        for cell in row.get("content") or []:
            if not isinstance(cell, dict):
                continue
            attrs = cell.get("attrs") or {}
            tag = "th" if cell.get("type") == "tableHeader" else "td"
            span = ""
            colspan = _int_attr(attrs, "colspan")
            rowspan = _int_attr(attrs, "rowspan")
            if colspan > 1:
                span += ' colspan="%d"' % colspan
            if rowspan > 1:
                span += ' rowspan="%d"' % rowspan
            cells.append("<%s%s>%s</%s>" % (tag, span, cell_html(cell), tag))
        return "<tr>%s</tr>" % "".join(cells)

    head = rows[0]
    has_head = any(isinstance(c, dict) and c.get("type") == "tableHeader"
                   for c in head.get("content") or [])
    parts: list[str] = []
    if has_head:
        parts.append("<thead>%s</thead>" % row_html(head))
        body = rows[1:]
    else:
        body = rows
    if body:
        parts.append("<tbody>%s</tbody>" % "".join(row_html(r) for r in body))
    return "<table>%s</table>" % "".join(parts)


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
        if kind == "table":
            return _render_md_table(node, cell_md)
        return inner

    def cell_md(cell) -> str:
        """单元格拍平成单行：段落直接取行内内容，去掉换行并转义管道符。"""
        chunks: list[str] = []
        for child in cell.get("content") or []:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "paragraph":
                chunks.append("".join(render(x) for x in child.get("content") or []))
            else:
                chunks.append(render(child))
        text = " ".join(c.strip() for c in chunks if c.strip())
        return text.replace("|", "\\|").replace("\n", " ").strip()

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
        if kind == "table":
            return _render_html_table(node, cell_html)
        return inner

    def cell_html(cell) -> str:
        """单元格内段落去掉 <p> 包裹，避免默认边距把表格撑开。"""
        chunks: list[str] = []
        for child in cell.get("content") or []:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "paragraph":
                chunks.append("".join(render(x) for x in child.get("content") or []))
            else:
                chunks.append(render(child))
        return "".join(chunks)

    body = "".join(render(c) for c in _parse_doc(content_json).get("content") or [])
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>%s</title><style>%s</style></head><body><h1>%s</h1>%s</body></html>"
            % (html.escape(title), EXPORT_STYLE, html.escape(title), body))


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
                              project_id, updated_at, substr(content_text,1,120) AS excerpt
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
        # sort_order 由前端拖拽排序写入；新建时缺省排到末尾，避免全部挤在 0 上
        order = body.sort_order if body.sort_order > 0 else conn.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM sections WHERE notebook_id=?",
            (body.notebook_id,)).fetchone()[0]
        sid = conn.execute(
            "INSERT INTO sections(notebook_id,name,sort_order) VALUES(?,?,?)",
            (body.notebook_id, name, order)).lastrowid
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

_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _pretty_name(raw: str, ext: str) -> str:
    """「另存为」用的建议文件名：去 Windows 非法字符 + 补日期。"""
    base = _UNSAFE_NAME.sub("_", (raw or "").strip()).strip(" .")[:80] or "onlyone"
    return "%s-%s.%s" % (base, datetime.now().strftime("%Y%m%d"), ext)


def _export_payload(scope: str, ident: int, fmt: str
                    ) -> tuple[bytes, str, str, str]:
    """构造导出内容，返回 ``(字节, media_type, ASCII 文件名, 建议文件名)``。

    HTTP 头的文件名只能是 latin-1，所以浏览器下载仍用 ``note-3.md`` 这类 ASCII 名；
    桌面端另存为用 ``_pretty_name`` 带中文标题，用户看得懂。
    """
    if scope not in ("page", "section", "notebook") or fmt not in ("md", "html", "zip"):
        raise HTTPException(400, "scope / format 不合法")
    conn = connect()
    try:
        if scope == "page":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE id=?", (ident,))]
            label = "note-%d" % ident
            row = conn.execute("SELECT title FROM notes WHERE id=?", (ident,)).fetchone()
            display = row["title"] if row else label
        elif scope == "section":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE section_id=? ORDER BY sort_order,id", (ident,))]
            label = "section-%s" % ident
            row = conn.execute("SELECT name FROM sections WHERE id=?", (ident,)).fetchone()
            display = row["name"] if row else label
        else:
            rows = [dict(r) for r in conn.execute(
                """SELECT n.* FROM notes n JOIN sections s ON n.section_id=s.id
                   WHERE s.notebook_id=? ORDER BY s.sort_order, n.sort_order, n.id""", (ident,))]
            label = "notebook-%s" % ident
            row = conn.execute("SELECT name FROM notebooks WHERE id=?", (ident,)).fetchone()
            display = row["name"] if row else label
    finally:
        conn.close()
    if not rows:
        raise HTTPException(404, "该范围没有页面")

    if fmt == "md":
        text = "\n\n---\n\n".join(
            _to_markdown(r["title"], r["content_json"]) for r in rows)
        return (text.encode("utf-8"), "text/markdown; charset=utf-8",
                label + ".md", _pretty_name(display, "md"))

    html_doc = "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">" \
               "<title>%s</title></head><body>%s</body></html>" % (
                   html.escape(display),
                   "".join(_to_html(r["title"], r["content_json"]) for r in rows))
    if fmt == "html":
        return (html_doc.encode("utf-8"), "text/html; charset=utf-8",
                label + ".html", _pretty_name(display, "html"))

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
    return (buffer.getvalue(), "application/zip",
            label + ".zip", _pretty_name(display, "zip"))


@router.get("/api/notes/export")
def export_notes(scope: str = "page", id: int = 0, format: str = "md"):
    """浏览器模式导出（HTTP 直接回字节）。桌面端请用 /api/notes/export/save。"""
    data, media_type, ascii_name, _ = _export_payload(scope, id, format)
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": 'attachment; filename="%s"' % ascii_name})


class ExportSaveIn(BaseModel):
    scope: str = "page"
    format: str = "md"
    id: int = 0


@router.post("/api/notes/export/save")
def export_notes_save(body: ExportSaveIn):
    """桌面端导出：弹系统「另存为」→ 后端写盘 → 返回真实路径。

    pywebview 默认禁止下载（``ALLOW_DOWNLOADS=False``），前端 ``<a download>``
    在窗口里点了没有任何反应；这里改成后端写盘，路径由用户自己选。
    没有桌面壳（浏览器模式 / --no-window）时返回 409，前端回退到 HTTP 下载。
    """
    if not desktop.has_save_dialog():
        raise HTTPException(409, "当前运行模式不支持另存为对话框，请改用下载接口")
    data, _, _, suggested = _export_payload(body.scope, body.id, body.format)
    target = desktop.ask_save_path(suggested)
    if not target:
        return {"saved": None, "canceled": True}
    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as exc:
        raise HTTPException(500, "写入失败：%s" % exc) from exc
    log.info("导出完成 %s（%d 字节）", path, len(data))
    return {"saved": str(path), "size": len(data), "canceled": False}


# ---------------------------------------------------------------- OneNote 导入

MAX_IMPORT_BYTES = 64 * 1024 * 1024       # 单个 .mht 上限
MAX_IMPORT_FILES = 20


def _ensure_notebook(conn, notebook_id: int | None, fallback_name: str) -> int:
    if notebook_id is not None:
        if conn.execute("SELECT 1 FROM notebooks WHERE id=?", (notebook_id,)).fetchone() is None:
            raise HTTPException(404, "笔记本不存在")
        return notebook_id
    name = (fallback_name or "").strip() or "SAP 笔记"
    row = conn.execute("SELECT id FROM notebooks WHERE name=? ORDER BY id LIMIT 1",
                       (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    return conn.execute(
        "INSERT INTO notebooks(name,sort_order) VALUES(?, "
        "(SELECT COALESCE(MAX(sort_order),0)+1 FROM notebooks))", (name,)).lastrowid


def _create_section_unique(conn, notebook_id: int, base_name: str) -> tuple[int, str]:
    """同名分区已存在时加序号，避免重复导入把页面混进旧分区。"""
    base = (base_name or "").strip() or "导入分区"
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sections WHERE notebook_id=?", (notebook_id,))}
    name = base
    index = 2
    while name in names:
        name = "%s (%d)" % (base, index)
        index += 1
    section_id = conn.execute(
        "INSERT INTO sections(notebook_id,name,sort_order) VALUES(?,?, "
        "(SELECT COALESCE(MAX(sort_order),0)+1 FROM sections WHERE notebook_id=?))",
        (notebook_id, name, notebook_id)).lastrowid
    return section_id, name


def _materialize_images(note_id: int, doc: dict,
                        images: dict[str, tuple[bytes, str]]
                        ) -> tuple[dict, int, list[str]]:
    """把 ``__mht__:xxx`` 占位换成真实附件 URL 并落盘；缺失/超限的图片摘掉。"""
    folder = ATTACH_DIR / str(note_id)
    uploaded = 0
    missing: list[str] = []

    def walk(node) -> None:
        nonlocal uploaded
        children = node.get("content")
        if not isinstance(children, list):
            return
        kept: list = []
        for child in children:
            if isinstance(child, dict) and child.get("type") == "image":
                src = str((child.get("attrs") or {}).get("src") or "")
                if not src.startswith(mht_import.IMG_SCHEME):
                    kept.append(child)
                    continue
                name = src[len(mht_import.IMG_SCHEME):]
                item = images.get(name)
                raw = item[0] if item else b""
                ext = _image_ext(raw) if raw else None
                if not raw or ext is None or len(raw) > MAX_IMAGE_BYTES:
                    missing.append(name)
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                filename = "%s.%s" % (hashlib.sha256(raw).hexdigest()[:32], ext)
                target = folder / filename
                if not target.is_file():
                    target.write_bytes(raw)
                child["attrs"]["src"] = "/api/attachments/%d/%s" % (note_id, filename)
                uploaded += 1
                kept.append(child)
                continue
            if isinstance(child, dict):
                walk(child)
                # 图片被摘掉后段落空了就一并去掉
                if child.get("type") == "paragraph" and not child.get("content"):
                    continue
            kept.append(child)
        node["content"] = kept

    walk(doc)
    return doc, uploaded, missing


@router.post("/api/notes/import-mht")
def import_mht(body: MhtImportIn):
    """把 OneNote 导出的 .mht 批量导入：一个文件 = 一个分区，一页 = 一个页面。

    base64 传输（与图片上传一致，不引入 python-multipart）。逐页提交，单页失败
    不影响已导入内容；同名分区自动加序号。
    """
    if not body.files:
        raise HTTPException(400, "没有待导入的文件")
    if len(body.files) > MAX_IMPORT_FILES:
        raise HTTPException(400, "单次最多导入 %d 个文件" % MAX_IMPORT_FILES)

    conn = connect()
    sections: list[dict] = []
    errors: list[dict] = []
    missing: list[str] = []
    total_pages = 0
    total_images = 0
    try:
        notebook_id = _ensure_notebook(conn, body.notebook_id, body.book_name)
        for item in body.files:
            display = (Path(item.name or "").name or "未命名").strip() or "未命名"
            stem = Path(display).stem or display
            try:
                raw = base64.b64decode(item.data, validate=True)
            except Exception:                    # noqa: BLE001
                errors.append({"file": display, "error": "base64 解码失败"})
                continue
            if not raw:
                errors.append({"file": display, "error": "文件为空"})
                continue
            if len(raw) > MAX_IMPORT_BYTES:
                errors.append({"file": display, "error": "超过 64 MB 上限"})
                continue
            try:
                pages, images = mht_import.build_pages(raw)
            except Exception as exc:             # noqa: BLE001
                errors.append({"file": display, "error": "解析失败：%s" % exc})
                continue
            if not pages:
                errors.append({"file": display, "error": "没有识别到页面"})
                continue

            section_id, section_name = _create_section_unique(conn, notebook_id, stem)
            page_count = 0
            first_note_id: int | None = None
            for title, doc in pages:
                note_id = conn.execute(
                    "INSERT INTO notes(section_id,title,sort_order) VALUES(?,?, "
                    "(SELECT COALESCE(MAX(sort_order),0)+1 FROM notes WHERE section_id=?))",
                    (section_id, title, section_id)).lastrowid
                if first_note_id is None:
                    first_note_id = int(note_id)
                doc, uploaded, miss = _materialize_images(note_id, doc, images)
                total_images += uploaded
                missing.extend(miss)
                content_json = json.dumps(doc, ensure_ascii=False)
                text = _extract_text(content_json)
                conn.execute("UPDATE notes SET content_json=?, content_text=? WHERE id=?",
                             (content_json, text, note_id))
                row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
                _sync_note_entry(conn, row)
                conn.commit()
                page_count += 1
            total_pages += page_count
            sections.append({"id": section_id, "name": section_name,
                             "pages": page_count, "first_note_id": first_note_id})
        _checkpoint(conn)
        return {"notebook_id": notebook_id, "sections": sections, "pages": total_pages,
                "images": total_images, "missing": missing, "errors": errors}
    finally:
        conn.close()


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
            "INSERT INTO notes(section_id,title,project_id,sort_order) VALUES(?,?,?, "
            "(SELECT COALESCE(MAX(sort_order),0)+1 FROM notes WHERE section_id=?))",
            (body.section_id, body.title.strip() or "未命名页面",
             body.project_id, body.section_id)).lastrowid
        row = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        _sync_note_entry(conn, row)
        conn.commit()
        return {"id": nid, "revision": 0}
    finally:
        conn.close()


@router.get("/api/notes")
def list_notes(project_id: int | None = None, include_private: bool = False):
    """按项目列页面（项目工作台用）；私密页默认不出现——它同样不进任何聚合视图。"""
    conn = connect()
    try:
        sql = """SELECT n.id, n.title, n.updated_at, n.is_private, n.project_id,
                        substr(n.content_text,1,160) AS excerpt,
                        s.name AS section_name, b.name AS notebook_name
                 FROM notes n
                 JOIN sections s ON s.id = n.section_id
                 JOIN notebooks b ON b.id = s.notebook_id"""
        where: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            where.append("n.project_id=?")
            params.append(project_id)
        if not include_private:
            where.append("n.is_private=0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n.updated_at DESC, n.id DESC LIMIT 500"
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


class ProjectNoteIn(BaseModel):
    title: str = "未命名页面"


@router.post("/api/projects/{project_id}/notes")
def create_project_note(project_id: int, body: ProjectNoteIn):
    """从项目工作台新建页面：自动落到「项目笔记」笔记本下按项目名命名的分区。"""
    conn = connect()
    try:
        proj = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
        if proj is None:
            raise HTTPException(404, "项目不存在：%s" % project_id)
        pname = str(proj["name"]).strip() or ("项目 %d" % project_id)
        book = conn.execute("SELECT id FROM notebooks WHERE name=? ORDER BY id LIMIT 1",
                            (PROJECT_BOOK,)).fetchone()
        if book is None:
            bid = conn.execute(
                "INSERT INTO notebooks(name,sort_order) VALUES(?, "
                "(SELECT COALESCE(MAX(sort_order),0)+1 FROM notebooks))",
                (PROJECT_BOOK,)).lastrowid
        else:
            bid = int(book["id"])
        sec = conn.execute("SELECT id FROM sections WHERE notebook_id=? AND name=?",
                           (bid, pname)).fetchone()
        if sec is None:
            sid = conn.execute(
                "INSERT INTO sections(notebook_id,name,sort_order) VALUES(?,?, "
                "(SELECT COALESCE(MAX(sort_order),0)+1 FROM sections WHERE notebook_id=?))",
                (bid, pname, bid)).lastrowid
        else:
            sid = int(sec["id"])
        nid = conn.execute(
            "INSERT INTO notes(section_id,title,project_id) VALUES(?,?,?)",
            (sid, body.title.strip() or "未命名页面", project_id)).lastrowid
        row = conn.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        _sync_note_entry(conn, row)
        conn.commit()
        return {"id": nid, "section_id": sid, "notebook_id": bid, "revision": 0}
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
    """base64 上传（无需 python-multipart）；校验魔数，避免伪装脚本文件。

    文件名 = ``sha256(raw)[:32].<ext>``，命中既有文件即复用（页内去重），
    返回 ``dedup`` 标明是否为复用。
    """
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
    # 文件名即内容哈希（前 32 位 hex）——仍满足 SAFE_FILENAME 白名单，且天然幂等：
    # 同一页面重复粘贴同一张图只落一份文件。去重范围刻意限定在 note_id 目录内，
    # 因为删页会 rmtree 整个目录，跨页复用会让删 A 页时把 B 页的图一起删掉。
    filename = "%s.%s" % (hashlib.sha256(raw).hexdigest()[:32], ext)
    folder = ATTACH_DIR / str(note_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    dedup = target.is_file()
    if not dedup:
        target.write_bytes(raw)
    return {"url": "/api/attachments/%d/%s" % (note_id, filename),
            "filename": filename, "size": len(raw), "dedup": dedup}


@router.get("/api/attachments/{note_id}/{filename}")
def get_attachment(note_id: int, filename: str):
    if not SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(400, "非法文件名")
    path = ATTACH_DIR / str(note_id) / filename
    if not path.is_file():
        raise HTTPException(404, "附件不存在")
    return FileResponse(str(path))
