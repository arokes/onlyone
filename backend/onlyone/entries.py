# -*- coding: utf-8 -*-
"""统一条目模型（M3-2）：业务记录与文件共用 ``entries`` + ``fts_entries``。

每类业务表在 ENTITIES 中登记：可写字段、参与检索的字段、时间字段与默认标题规则。
业务记录增删改后必须调用 ``sync_entry`` / ``drop_entry``，否则全局搜索看不到它们。
凭据表 ``credentials`` 不在此登记——按 v1.0 §2 硬约束 3，凭据永不进 FTS。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import semantic


@dataclass(frozen=True)
class Entity:
    table: str
    type: str
    label: str
    columns: tuple[str, ...]
    body_fields: tuple[str, ...]
    date_field: str
    defaults: dict = field(default_factory=dict)


ENTITIES: dict[str, Entity] = {
    "projects": Entity(
        table="projects", type="project", label="项目事项",
        columns=("name", "status", "note"),
        body_fields=("note", "status"), date_field="created_at",
        defaults={"status": "active"},
    ),
    "links": Entity(
        table="links", type="link", label="企微链接",
        columns=("title", "url", "project_id", "note"),
        body_fields=("url", "note"), date_field="last_check_at",
    ),
    "worklogs": Entity(
        table="worklogs", type="worklog", label="工作日志",
        columns=("log_date", "project_id", "content", "minutes"),
        body_fields=("content",), date_field="log_date",
    ),
    "incidents": Entity(
        table="incidents", type="incident", label="运维记录",
        columns=("occurred_at", "system", "symptom", "root_cause",
                 "action", "status", "duration_min", "project_id", "note"),
        body_fields=("system", "symptom", "root_cause", "action", "note"),
        date_field="occurred_at", defaults={"status": "open"},
    ),
    # M7：只供 notes.py 复用统一条目同步；business.py 的通用 CRUD 会排除它
    # （自动保存有 revision 乐观锁与私密页逻辑，不能从 /api/biz/notes 绕过）。
    "notes": Entity(
        table="notes", type="note", label="记事本",
        columns=("section_id", "title", "content_json", "content_text",
                 "sort_order", "is_private", "revision"),
        body_fields=("content_text",), date_field="updated_at",
    ),
}

NUMERIC_FIELDS = {"project_id", "minutes", "duration_min"}


def _get(row, key: str):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return ""
    return "" if value is None else value


def _title(kind: str, row) -> str:
    if kind == "projects":
        return str(_get(row, "name")).strip() or "未命名项目"
    if kind == "links":
        return str(_get(row, "title")).strip() or "未命名链接"
    if kind == "worklogs":
        text = " ".join(str(_get(row, "content")).split())[:24]
        return ("工作日志 %s %s" % (_get(row, "log_date"), text)).strip()
    if kind == "incidents":
        system = str(_get(row, "system")).strip() or "未分类系统"
        symptom = " ".join(str(_get(row, "symptom")).split())[:30]
        return "%s · %s" % (system, symptom or "运维记录")
    if kind == "notes":
        return str(_get(row, "title")).strip() or "未命名页面"
    return str(_get(row, "id"))


def make_body(kind: str, row) -> str:
    parts = []
    for col in ENTITIES[kind].body_fields:
        value = str(_get(row, col)).strip()
        if value:
            parts.append(value)
    return "\n".join(parts)


def sync_entry(conn: sqlite3.Connection, kind: str, row) -> int:
    """新建或更新统一条目，返回 entry_id。幂等，可在事务内调用。"""
    entity = ENTITIES[kind]
    ref_id = int(row["id"])
    title = _title(kind, row)
    body = make_body(kind, row)
    summary = " ".join(body.split())[:200]
    occurred = str(_get(row, entity.date_field))[:19] or None
    project_id = _get(row, "project_id") or None

    found = conn.execute(
        "SELECT id FROM entries WHERE type=? AND ref_id=?", (entity.type, ref_id)
    ).fetchone()
    if found:
        eid = found["id"]
        conn.execute(
            """UPDATE entries SET title=?, summary=?, occurred_at=?, tags=?, project_id=?,
                       updated_at=datetime('now','localtime') WHERE id=?""",
            (title, summary, occurred, entity.label, project_id, eid),
        )
        # 只有更新路径才有旧正文要删。fts_entries.entry_id 是 UNINDEXED 列，
        # DELETE WHERE entry_id=? 会全表扫描（本机 6291 行实测 125~175ms）；
        # 新建条目的 eid 刚生成，不可能有旧行，跳过可省掉这次扫描。
        conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (eid,))
    else:
        eid = conn.execute(
            """INSERT INTO entries(type,ref_id,title,summary,occurred_at,tags,project_id)
               VALUES(?,?,?,?,?,?,?)""",
            (entity.type, ref_id, title, summary, occurred, entity.label, project_id),
        ).lastrowid
    conn.execute("INSERT INTO fts_entries(entry_id,title,body,tags) VALUES(?,?,?,?)",
                 (eid, title, body or title, entity.label))
    semantic.sync_entry(conn, eid, title, body)
    return eid


def drop_entry(conn: sqlite3.Connection, kind: str, ref_id: int) -> None:
    """删除业务记录对应的统一条目及其关联关系。"""
    entity = ENTITIES[kind]
    found = conn.execute(
        "SELECT id FROM entries WHERE type=? AND ref_id=?", (entity.type, ref_id)
    ).fetchone()
    if not found:
        return
    eid = found["id"]
    conn.execute("DELETE FROM entry_relations WHERE src_entry_id=? OR dst_entry_id=?", (eid, eid))
    conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (eid,))
    semantic.drop_entry(conn, eid)
    conn.execute("DELETE FROM entries WHERE id=?", (eid,))


def unlink_project(conn: sqlite3.Connection, project_id: int) -> None:
    """删除项目后，解除其余业务记录对它的引用并重同步条目，避免悬空 project_id。"""
    for kind in ("links", "worklogs", "incidents"):
        table = ENTITIES[kind].table
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM {table} WHERE project_id=?", (project_id,))]
        if not ids:
            continue
        conn.execute(f"UPDATE {table} SET project_id=NULL WHERE project_id=?", (project_id,))
        for rid in ids:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
            if row:
                sync_entry(conn, kind, row)
