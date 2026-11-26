# -*- coding: utf-8 -*-
"""用户标签（2026-11-25，功能 1）。

标签挂在**统一条目**上（`entries.user_tags`），因此文件、笔记、项目、运维记录、链接
共用一套标签，可以跨类型聚在一起。与 `entries.tags` 严格分开：后者存业务类型
（"运维记录"/"文件"），是系统标记，不参与用户编辑。

存储：JSON 数组字符串（`["MRP","月度结账"]`），用 SQLite 的 `json_each` 聚合，
不新增关联表。约束：单个标签 ≤20 字、每个条目 ≤10 个、自动去重。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .db import connect

log = logging.getLogger("onlyone")
router = APIRouter()

MAX_TAGS = 10
MAX_TAG_LEN = 20
MAX_LIST = 100


class TagsIn(BaseModel):
    tags: list[str] = []


def parse_tags(raw) -> list[str]:
    """把列值解析成标签数组；历史行可能是空串或非法 JSON，一律按空处理。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(t).strip() for t in data if str(t).strip()]


@router.get("/api/tags")
def list_tags(limit: int = MAX_LIST):
    """标签清单 + 使用次数，按次数降序（侧栏/搜索页标签栏用）。"""
    limit = max(1, min(int(limit), 500))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT j.value AS tag, count(*) AS count
               FROM entries e, json_each(e.user_tags) j
               WHERE j.value IS NOT NULL AND j.value <> ''
               GROUP BY j.value
               ORDER BY count DESC, j.value
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


@router.put("/api/entries/{entry_id}/tags")
def set_tags(entry_id: int, body: TagsIn):
    """整组覆盖该条目的标签（编辑弹窗一次提交全部）。"""
    cleaned: list[str] = []
    for raw in body.tags[:MAX_TAGS * 2]:
        tag = str(raw or "").strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise HTTPException(400, "标签最长 %d 个字符：%s" % (MAX_TAG_LEN, tag))
        if tag not in cleaned:
            cleaned.append(tag)
    if len(cleaned) > MAX_TAGS:
        raise HTTPException(400, "每个条目最多 %d 个标签" % MAX_TAGS)

    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone() is None:
            raise HTTPException(404, "条目不存在：%s" % entry_id)
        conn.execute(
            """UPDATE entries SET user_tags=?, updated_at=datetime('now','localtime')
               WHERE id=?""",
            (json.dumps(cleaned, ensure_ascii=False), entry_id),
        )
        conn.commit()
        return {"entry_id": entry_id, "tags": cleaned}
    finally:
        conn.close()
