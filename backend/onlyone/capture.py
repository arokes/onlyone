# -*- coding: utf-8 -*-
"""快速捕获（2026-11-25，功能 6）。

前端轮询 ``/api/capture/pending``：热键线程把标志置真后，这里取走并返回"触发前的
前台窗口句柄"，供保存/取消后把焦点还回去。

写入复用既有链路，不另开一套：
* 运维记录 → ``business.create_biz``（自动 sync_entry，进检索）；
* 笔记 → ``notes.create_note`` + ``notes.update_note``（自动建 entries 与 FTS）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import desktop, hotkey
from .business import BizIn, create_biz
from .db import connect
from .notes import (NoteCreate, NoteUpdate, create_note, persist_inline_images,
                    update_note)

log = logging.getLogger("onlyone")
router = APIRouter()


class CaptureIn(BaseModel):
    kind: str = "incident"          # incident | note
    text: str
    section_id: int | None = None   # 落点分区；不传则用默认目标
    content_json: str | None = None  # 笔记富文本（含 base64 内联图）；不传则按纯文本存
    title: str | None = None
    fore: int | None = None
    category: str | None = None
    system: str | None = None
    project_id: int | None = None


class DismissIn(BaseModel):
    fore: int | None = None


class TargetIn(BaseModel):
    section_id: int


TARGET_KEY = "capture_target"


def _read_target(conn) -> int | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (TARGET_KEY,)).fetchone()
    if not row or not row["value"]:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _notebook_tree(conn) -> list[dict]:
    books = conn.execute(
        "SELECT id, name FROM notebooks ORDER BY sort_order, id").fetchall()
    tree = []
    for book in books:
        sections = conn.execute(
            "SELECT id, name FROM sections WHERE notebook_id=? ORDER BY sort_order, id",
            (book["id"],)).fetchall()
        tree.append({
            "id": book["id"],
            "name": book["name"],
            "sections": [{"id": s["id"], "name": s["name"]} for s in sections],
        })
    return tree


@router.get("/api/capture/target")
def capture_target():
    """当前速记落点 + 可选的分区树。未设置时回退到第一个笔记本的第一个分区。"""
    conn = connect()
    try:
        tree = _notebook_tree(conn)
        chosen = _read_target(conn)
        flat = [(b["id"], b["name"], s["id"], s["name"])
                for b in tree for s in b["sections"]]
        hit = next((item for item in flat if item[2] == chosen), None) or (flat[0] if flat else None)
        if hit is None:
            return {"section_id": None, "notebook_id": None,
                    "notebook_name": "", "section_name": "", "notebooks": tree}
        return {"section_id": hit[2], "notebook_id": hit[0],
                "notebook_name": hit[1], "section_name": hit[3], "notebooks": tree}
    finally:
        conn.close()


@router.put("/api/capture/target")
def set_capture_target(body: TargetIn):
    conn = connect()
    try:
        row = conn.execute(
            """SELECT s.id, s.name AS section_name, n.id AS notebook_id, n.name AS notebook_name
               FROM sections s JOIN notebooks n ON n.id = s.notebook_id
               WHERE s.id=?""", (body.section_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "分区不存在")
        conn.execute(
            """INSERT INTO app_meta(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (TARGET_KEY, str(body.section_id)))
        conn.commit()
        return {"section_id": row["id"], "section_name": row["section_name"],
                "notebook_id": row["notebook_id"], "notebook_name": row["notebook_name"]}
    finally:
        conn.close()


@router.get("/api/hotkey/status")
def hotkey_status():
    """侧栏底部用来提示「热键不可用」以及当前组合。"""
    state = hotkey.status()
    return {"enabled": state["enabled"], "hotkey": state["hotkey"],
            "error": state["error"]}


@router.get("/api/capture/pending")
def capture_pending():
    opened, fore = hotkey.consume()
    return {"open": opened, "fore": fore, "enabled": hotkey.status().get("enabled", False)}


@router.post("/api/capture/_trigger")
def capture_trigger():
    """仅供自动化测试模拟热键；未设置 ONLYONE_DEV_HOTKEY=1 时直接 404。

    真实热键由 Win32 线程注册（hotkey.py），测试环境没有桌面消息循环，
    所以留这一个开关，避免为了截图去伪造键盘事件。
    """
    if os.environ.get("ONLYONE_DEV_HOTKEY") != "1":
        raise HTTPException(404, "not found")
    hotkey.poke()
    return {"ok": True}


@router.post("/api/capture/dismiss")
def capture_dismiss(body: DismissIn):
    desktop.finish_capture(body.fore)
    return {"ok": True}


def _default_section() -> int | None:
    conn = connect()
    try:
        return _read_target(conn)
    finally:
        conn.close()


def _ensure_note_target() -> int:
    """找（或建）一个「收件箱 / 快速捕获」分区，供无处可去的速记落位。"""
    conn = connect()
    try:
        book = conn.execute("SELECT id FROM notebooks ORDER BY id LIMIT 1").fetchone()
        if book is None:
            book_id = conn.execute(
                "INSERT INTO notebooks(name) VALUES('收件箱')").lastrowid
        else:
            book_id = book["id"]
        section = conn.execute(
            "SELECT id FROM sections WHERE notebook_id=? ORDER BY id LIMIT 1",
            (book_id,)).fetchone()
        if section is None:
            section_id = conn.execute(
                "INSERT INTO sections(notebook_id, name) VALUES(?, '快速捕获')",
                (book_id,)).lastrowid
        else:
            section_id = section["id"]
        conn.commit()
        return int(section_id)
    finally:
        conn.close()


@router.post("/api/capture")
def capture_create(body: CaptureIn):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    try:
        if body.kind == "note":
            text = text or "截图"
            section_id = body.section_id or _default_section() or _ensure_note_target()
            created = create_note(NoteCreate(
                section_id=section_id, title=(body.title or text)[:40]))
            note_id = created["id"]
            doc = None
            if body.content_json:
                try:
                    doc = json.loads(body.content_json)
                except (TypeError, ValueError):
                    doc = None
            if doc is None:
                doc = {"type": "doc", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
            # 内联截图在此时才落盘（先有 note_id 才能放进该页目录）
            images = persist_inline_images(note_id, doc)
            update_note(note_id, NoteUpdate(
                content_json=json.dumps(doc, ensure_ascii=False),
                revision=int(created.get("revision", 0))))
            desktop.finish_capture(body.fore)
            log.info("快速捕获 → 笔记 id=%s images=%s", note_id, images)
            return {"kind": "note", "id": note_id, "images": images}

        result = create_biz("incidents", BizIn(data={
            "occurred_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "category": (body.category or "新需求").strip() or "新需求",
            "system": (body.system or "").strip(),
            "symptom": text,
            "status": "open",
            "project_id": body.project_id,
        }))
        desktop.finish_capture(body.fore)
        log.info("快速捕获 → 运维记录 id=%s", result["id"])
        return {"kind": "incident", "id": result["id"], "entry_id": result.get("entry_id")}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("快速捕获失败：%s", exc)
        raise HTTPException(500, "写入失败：%s" % exc) from exc
