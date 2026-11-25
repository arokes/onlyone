# -*- coding: utf-8 -*-
"""业务记录 API（M3-1）：四张业务表通用 CRUD + 统一条目同步 + 关联关系。

所有表名 / 列名都来自 ENTITIES 白名单，不接受客户端传入的表名或列名。
企微链接只登记标题 + URL + 归属项目，不读取文档内容（v1.0 §2 硬约束 1）。
"""
from __future__ import annotations

import logging
import sqlite3
import webbrowser
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import rich, sap
from .db import connect, decrypt
from .entries import ENTITIES, NUMERIC_FIELDS, Entity, drop_entry, sync_entry, unlink_project

log = logging.getLogger("onlyone")
router = APIRouter()


class BizIn(BaseModel):
    data: dict[str, Any]


class RelationIn(BaseModel):
    src_entry_id: int
    dst_entry_id: int
    relation: str = "related"


class ProjectLinkIn(BaseModel):
    project_id: int


class OpenLinkIn(BaseModel):
    url: str


# M7：notes 由专用路由管理（自动保存有 revision 乐观锁与私密页逻辑），
# 不能从通用 CRUD 绕过，否则私密页可能被写进 FTS。
ROUTER_EXCLUDED = {"notes"}


def _entity(kind: str) -> Entity:
    if kind in ROUTER_EXCLUDED:
        raise HTTPException(404, "该类型由专用路由管理：%s" % kind)
    entity = ENTITIES.get(kind)
    if entity is None:
        raise HTTPException(404, "未知业务类型：%s" % kind)
    return entity


# 表结构元数据（进程内缓存）：建表语句里的 NOT NULL / DEFAULT 是「未填值如何落库」的
# 唯一事实来源，避免为每张业务表手工维护一份可空列清单（1B 方案）。
_META_CACHE: dict[str, dict[str, tuple[bool, Any]]] = {}


def _parse_default(raw: Any) -> Any:
    """把 PRAGMA table_info 的 dflt_value 还原成 Python 值；表达式默认值返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    try:
        return int(text)
    except ValueError:
        return None  # datetime('now','localtime') 一类表达式交给 SQLite 自己填


def _column_meta(conn, table: str) -> dict[str, tuple[bool, Any]]:
    if table not in _META_CACHE:
        _META_CACHE[table] = {
            r["name"]: (bool(r["notnull"]), _parse_default(r["dflt_value"]))
            for r in conn.execute(f"PRAGMA table_info({table})")
        }
    return _META_CACHE[table]


def _null_value(meta: dict[str, tuple[bool, Any]], col: str) -> Any:
    """按列 DDL 归一「未填」：可空列→None；非空列→默认值 / 0 / 空串。"""
    notnull, default = meta.get(col, (False, None))
    if not notnull:
        return None
    if default is not None:
        return default
    return 0 if col in NUMERIC_FIELDS else ""


def _clean(entity: Entity, payload: dict[str, Any], creating: bool,
           meta: dict[str, tuple[bool, Any]]) -> dict[str, Any]:
    """只保留白名单字段；未填值按列 DDL 归一；数字字段整数化，字符串去首尾空白。"""
    out: dict[str, Any] = {}
    for col in entity.columns:
        if col in payload:
            value = payload[col]
            if value is None or (col in NUMERIC_FIELDS and value == ""):
                value = _null_value(meta, col)
            if col in NUMERIC_FIELDS and value is not None:
                try:
                    value = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, "%s 必须是整数" % col) from exc
            elif isinstance(value, str):
                value = value.strip()
            out[col] = value
        elif creating and col in entity.defaults:
            out[col] = entity.defaults[col]
    return out


def _validate(kind: str, data: dict[str, Any], creating: bool) -> None:
    def required(field: str, label: str) -> None:
        if (creating or field in data) and not str(data.get(field) or "").strip():
            raise HTTPException(400, "%s不能为空" % label)

    if kind == "projects":
        required("name", "项目名称")
    elif kind == "links":
        required("title", "链接标题")
        if creating or "url" in data:
            url = str(data.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise HTTPException(400, "链接必须以 http:// 或 https:// 开头")
    elif kind == "incidents":
        required("occurred_at", "发生时间")


def _row(conn, entity: Entity, rid: int):
    return conn.execute(f"SELECT * FROM {entity.table} WHERE id=?", (rid,)).fetchone()


def _entry_exists(conn, entry_id: int) -> bool:
    return conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is not None


@router.get("/api/biz/{kind}")
def list_biz(kind: str, project_id: int | None = None):
    entity = _entity(kind)
    conn = connect()
    try:
        sql = (f"SELECT b.*, e.id AS entry_id FROM {entity.table} b "
               "LEFT JOIN entries e ON e.type=? AND e.ref_id=b.id")
        params: list[Any] = [entity.type]
        if project_id is not None and "project_id" in entity.columns:
            sql += " WHERE b.project_id=?"
            params.append(project_id)
        sql += f" ORDER BY b.{entity.date_field} DESC, b.id DESC"
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


@router.get("/api/biz/{kind}/{rid}")
def get_biz(kind: str, rid: int):
    entity = _entity(kind)
    conn = connect()
    try:
        row = _row(conn, entity, rid)
        if row is None:
            raise HTTPException(404, "记录不存在：%s/%s" % (kind, rid))
        return dict(row)
    finally:
        conn.close()


@router.post("/api/biz/{kind}")
def create_biz(kind: str, body: BizIn):
    entity = _entity(kind)
    conn = connect()
    try:
        data = _clean(entity, body.data, creating=True,
                      meta=_column_meta(conn, entity.table))
        _validate(kind, data, creating=True)
        if not data:
            raise HTTPException(400, "没有可写入的字段")
        cols = list(data.keys())
        sql = (f"INSERT INTO {entity.table} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' for _ in cols)})")
        try:
            rid = conn.execute(sql, [data[c] for c in cols]).lastrowid
            eid = sync_entry(conn, kind, _row(conn, entity, rid))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.warning("写入 %s 失败：%s", kind, exc)
            raise HTTPException(400, "写入失败：%s" % exc) from exc
        return {"id": rid, "entry_id": eid}
    finally:
        conn.close()


@router.put("/api/biz/{kind}/{rid}")
def update_biz(kind: str, rid: int, body: BizIn):
    entity = _entity(kind)
    conn = connect()
    try:
        data = _clean(entity, body.data, creating=False,
                      meta=_column_meta(conn, entity.table))
        _validate(kind, data, creating=False)
        if _row(conn, entity, rid) is None:
            raise HTTPException(404, "记录不存在：%s/%s" % (kind, rid))
        try:
            if data:
                conn.execute(
                    f"UPDATE {entity.table} SET {', '.join(c + '=?' for c in data)} WHERE id=?",
                    [*data.values(), rid],
                )
            eid = sync_entry(conn, kind, _row(conn, entity, rid))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            log.warning("更新 %s/%s 失败：%s", kind, rid, exc)
            raise HTTPException(400, "保存失败：%s" % exc) from exc
        return {"id": rid, "entry_id": eid}
    finally:
        conn.close()


@router.delete("/api/biz/{kind}/{rid}")
def delete_biz(kind: str, rid: int):
    entity = _entity(kind)
    conn = connect()
    try:
        if _row(conn, entity, rid) is None:
            raise HTTPException(404, "记录不存在：%s/%s" % (kind, rid))
        drop_entry(conn, kind, rid)
        if kind == "projects":
            unlink_project(conn, rid)
        conn.execute(f"DELETE FROM {entity.table} WHERE id=?", (rid,))
        conn.commit()
        return {"deleted": rid}
    finally:
        conn.close()


@router.get("/api/projects/{project_id}/workspace")
def project_workspace(project_id: int, limit: int = 5):
    """项目工作台：一次返回五类计数 + 各类最近 N 条（页签徽标与摘要共用一次请求）。

    * 记事本只统计**非私密页**，与 ``/api/notes?project_id=`` 口径一致。
    * 关联文件来自 ``entry_relations`` 的 ``relation='project'``（在搜索页手动关联）。
    """
    entity = ENTITIES["projects"]
    conn = connect()
    try:
        project = _row(conn, entity, project_id)
        if project is None:
            raise HTTPException(404, "项目不存在：%s" % project_id)
        limit = max(0, min(int(limit), 50))
        counts = {
            "links": conn.execute("SELECT COUNT(*) FROM links WHERE project_id=?",
                                  (project_id,)).fetchone()[0],
            "incidents": conn.execute("SELECT COUNT(*) FROM incidents WHERE project_id=?",
                                      (project_id,)).fetchone()[0],
            "notes": conn.execute(
                "SELECT COUNT(*) FROM notes WHERE project_id=? AND is_private=0",
                (project_id,)).fetchone()[0],
        }
        prow = conn.execute("SELECT id FROM entries WHERE type='project' AND ref_id=?",
                            (project_id,)).fetchone()
        pid = int(prow["id"]) if prow else 0
        if pid:
            counts["files"] = conn.execute(
                """SELECT COUNT(*) FROM entry_relations r
                   JOIN entries e ON e.id = CASE WHEN r.src_entry_id=? THEN r.dst_entry_id
                                                 ELSE r.src_entry_id END
                   WHERE (r.src_entry_id=? OR r.dst_entry_id=?)
                     AND r.relation='project' AND e.type='file'""",
                (pid, pid, pid)).fetchone()[0]
        else:
            counts["files"] = 0

        previews: dict[str, list[dict]] = {k: [] for k in
                                           ("links", "incidents", "notes", "files")}
        if limit:
            previews["links"] = [dict(r) for r in conn.execute(
                """SELECT id, title, url, note FROM links WHERE project_id=?
                   ORDER BY COALESCE(last_check_at,'') DESC, id DESC LIMIT ?""",
                (project_id, limit))]
            previews["incidents"] = [dict(r) for r in conn.execute(
                """SELECT id, occurred_at, category, system, proposer, company, symptom, status
                   FROM incidents
                   WHERE project_id=? ORDER BY occurred_at DESC, id DESC LIMIT ?""",
                (project_id, limit))]
            # symptom 是富文本列；预览卡片只要纯文本，避免把 JSON 结构漏到界面上
            for item in previews["incidents"]:
                item["symptom"] = rich.extract_text(item.get("symptom"))
            previews["notes"] = [dict(r) for r in conn.execute(
                """SELECT id, title, updated_at, substr(content_text,1,120) AS excerpt
                   FROM notes WHERE project_id=? AND is_private=0
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (project_id, limit))]
            if pid:
                previews["files"] = [dict(r) for r in conn.execute(
                    """SELECT e.id AS entry_id, e.title, e.summary, f.abs_path, f.ext
                       FROM entry_relations r
                       JOIN entries e ON e.id = CASE WHEN r.src_entry_id=? THEN r.dst_entry_id
                                                     ELSE r.src_entry_id END
                       LEFT JOIN files f ON f.id = e.ref_id
                       WHERE (r.src_entry_id=? OR r.dst_entry_id=?)
                         AND r.relation='project' AND e.type='file'
                       ORDER BY e.updated_at DESC, e.id DESC LIMIT ?""",
                    (pid, pid, pid, limit))]

        data = dict(project)
        data["entry_id"] = pid or None
        return {"project": data, "counts": counts, "previews": previews}
    finally:
        conn.close()


@router.get("/api/projects/{project_id}/files")
def project_files(project_id: int):
    """项目关联文件（在搜索页点「关联到项目」形成的 relation='project' 关系）。"""
    conn = connect()
    try:
        if _row(conn, ENTITIES["projects"], project_id) is None:
            raise HTTPException(404, "项目不存在：%s" % project_id)
        prow = conn.execute("SELECT id FROM entries WHERE type='project' AND ref_id=?",
                            (project_id,)).fetchone()
        if prow is None:
            return []
        pid = int(prow["id"])
        return [dict(r) for r in conn.execute(
            """SELECT e.id AS entry_id, e.title, e.summary, e.updated_at,
                      f.abs_path, f.ext, f.size
               FROM entry_relations r
               JOIN entries e ON e.id = CASE WHEN r.src_entry_id=? THEN r.dst_entry_id
                                             ELSE r.src_entry_id END
               LEFT JOIN files f ON f.id = e.ref_id
               WHERE (r.src_entry_id=? OR r.dst_entry_id=?)
                 AND r.relation='project' AND e.type='file'
               ORDER BY e.updated_at DESC, e.id DESC LIMIT 500""",
            (pid, pid, pid))]
    finally:
        conn.close()


@router.get("/api/relations")
def list_relations(entry_id: int):
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT r.src_entry_id, r.dst_entry_id, r.relation,
                      e.id AS peer_id, e.type AS peer_type,
                      e.title AS peer_title, e.summary AS peer_summary
               FROM entry_relations r
               JOIN entries e ON e.id = CASE
                    WHEN r.src_entry_id = ? THEN r.dst_entry_id ELSE r.src_entry_id END
               WHERE r.src_entry_id = ? OR r.dst_entry_id = ?
               ORDER BY r.relation, e.id""", (entry_id, entry_id, entry_id))
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/api/relations")
def add_relation(body: RelationIn):
    conn = connect()
    try:
        for eid in (body.src_entry_id, body.dst_entry_id):
            if not _entry_exists(conn, eid):
                raise HTTPException(404, "条目不存在：%s" % eid)
        dup = conn.execute(
            """SELECT 1 FROM entry_relations WHERE relation=? AND
               ((src_entry_id=? AND dst_entry_id=?) OR (src_entry_id=? AND dst_entry_id=?))""",
            (body.relation, body.src_entry_id, body.dst_entry_id,
             body.dst_entry_id, body.src_entry_id),
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO entry_relations(src_entry_id,dst_entry_id,relation) VALUES(?,?,?)",
                (body.src_entry_id, body.dst_entry_id, body.relation),
            )
        conn.commit()
        return {"ok": True, "duplicate": bool(dup)}
    finally:
        conn.close()


@router.delete("/api/relations")
def delete_relation(src_entry_id: int, dst_entry_id: int, relation: str = "related"):
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM entry_relations WHERE src_entry_id=? AND dst_entry_id=? AND relation=?",
            (src_entry_id, dst_entry_id, relation),
        )
        conn.commit()
        return {"deleted": [src_entry_id, dst_entry_id, relation]}
    finally:
        conn.close()


@router.post("/api/entries/{entry_id}/link-project")
def link_project(entry_id: int, body: ProjectLinkIn):
    """「关联到项目」闭环：把任意条目关联到项目条目。"""
    conn = connect()
    try:
        if not _entry_exists(conn, entry_id):
            raise HTTPException(404, "条目不存在：%s" % entry_id)
        prow = conn.execute(
            "SELECT id FROM entries WHERE type='project' AND ref_id=?", (body.project_id,)
        ).fetchone()
        if prow is None:
            raise HTTPException(404, "项目不存在或尚未建立条目：%s" % body.project_id)
        pid = prow["id"]
        if pid == entry_id:
            raise HTTPException(400, "不能把条目关联到自身")
        dup = conn.execute(
            """SELECT 1 FROM entry_relations WHERE relation='project' AND
               ((src_entry_id=? AND dst_entry_id=?) OR (src_entry_id=? AND dst_entry_id=?))""",
            (entry_id, pid, pid, entry_id),
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO entry_relations(src_entry_id,dst_entry_id,relation) "
                "VALUES(?,?,'project')", (entry_id, pid),
            )
        conn.commit()
        return {"ok": True, "duplicate": bool(dup), "project_entry_id": pid}
    finally:
        conn.close()


@router.delete("/api/entries/{entry_id}/link-project")
def unlink_project_entry(entry_id: int, project_id: int):
    conn = connect()
    try:
        prow = conn.execute(
            "SELECT id FROM entries WHERE type='project' AND ref_id=?", (project_id,)
        ).fetchone()
        if prow is None:
            raise HTTPException(404, "项目不存在：%s" % project_id)
        pid = prow["id"]
        conn.execute(
            """DELETE FROM entry_relations WHERE relation='project' AND
               ((src_entry_id=? AND dst_entry_id=?) OR (src_entry_id=? AND dst_entry_id=?))""",
            (entry_id, pid, pid, entry_id),
        )
        conn.commit()
        return {"deleted": [entry_id, pid]}
    finally:
        conn.close()


@router.post("/api/open-link")
def open_link(body: OpenLinkIn):
    """企微链接：只用系统默认浏览器打开，不读取文档内容。"""
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "只允许打开 http/https 链接")
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "打开失败：%s" % exc) from exc
    return {"opened": url}


# SAP GUI 实现集中在 sap.py：路径可配置 / 8.00 连接项名 / 窗口置前 / 启动校验。
class SapConfigIn(BaseModel):
    sap_gui_dir: str = ""
    landscape_xml: str = ""


@router.post("/api/open-sap")
def open_sap():
    """启动本机 SAP Logon；已在运行时把窗口置前，不再重复起进程。"""
    return sap.launch_logon()


@router.get("/api/sap/systems")
def sap_systems():
    return sap.list_systems()


@router.get("/api/sap/config")
def sap_config_get():
    return sap.get_config()


@router.put("/api/sap/config")
def sap_config_put(body: SapConfigIn):
    return sap.save_config(body.sap_gui_dir, body.landscape_xml)


@router.post("/api/sap/pick-dir")
def sap_pick_dir():
    """弹原生文件夹选择框；选中后直接保存，不改变 Landscape 配置。"""
    result = sap.pick_dir()
    if result.get("error"):
        raise HTTPException(400, result["error"])
    if result.get("cancelled"):
        return {"cancelled": True, "config": sap.get_config()}
    cfg = sap.save_config(gui_dir=result["path"])
    return {"cancelled": False, "path": result["path"], "config": cfg}


@router.post("/api/credentials/{cid}/open-sap")
def open_credential_sap(cid: int):
    """按凭据绑定的系统启动 SAP：自动填用户名，不填密码。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM credentials WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(404, "凭据不存在：%s" % cid)
        sap_conn = str(row["sap_conn"] or "")
        try:
            username = decrypt(row["username_enc"]) if row["username_enc"] else ""
        except Exception:  # noqa: BLE001 - 不回显任何解密细节
            username = ""
    finally:
        conn.close()
    return sap.launch_connection(sap_conn, username)
