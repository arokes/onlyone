# -*- coding: utf-8 -*-
"""保存视图（2026-11-25，功能 2）：把一组筛选条件命名保存，一键恢复。

存在 ``app_meta.saved_views``（JSON 数组），不新增表。视图保存的是**筛选值本身**，
不是查询结果 —— 打开时按当前数据重算，因此不会出现"视图里是上周的结果"。

设计取舍
--------
* 视图按 `tab` 归属（incidents / links / projects），只在该页签的筛选栏里出现，
  避免一个全局列表里混着互不兼容的字段；
* 只存非空条件（空字符串 / 空数组一律丢弃），否则视图恢复后会出现一堆空下拉；
* 最多 50 个，够用且能防止误操作刷爆 app_meta。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .db import connect

log = logging.getLogger("onlyone")
router = APIRouter()

META_KEY = "saved_views"
MAX_VIEWS = 50
MAX_NAME = 40


class ViewIn(BaseModel):
    name: str
    tab: str
    filters: dict = {}


class ViewUpdate(BaseModel):
    name: str | None = None
    filters: dict | None = None


def _load(conn) -> list[dict]:
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (META_KEY,)).fetchone()
    if not row or not row["value"]:
        return []
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        log.warning("app_meta.%s 不是合法 JSON，按空处理", META_KEY)
        return []
    return data if isinstance(data, list) else []


def _save(conn, views: list[dict]) -> None:
    conn.execute(
        """INSERT INTO app_meta(key, value) VALUES(?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (META_KEY, json.dumps(views, ensure_ascii=False)),
    )
    conn.commit()


def _clean_filters(raw: dict) -> dict:
    """只保留有意义的值：空串 / 空数组 / null 全部丢掉。"""
    out: dict = {}
    for key, value in (raw or {}).items():
        if not isinstance(key, str):
            continue
        if value is None or value == "":
            continue
        if isinstance(value, list):
            kept = [v for v in value if v not in (None, "")]
            if kept:
                out[key] = kept
            continue
        out[key] = value
    return out


@router.get("/api/views")
def list_views(tab: str | None = None):
    conn = connect()
    try:
        items = _load(conn)
    finally:
        conn.close()
    if tab:
        items = [v for v in items if v.get("tab") == tab]
    return items


@router.post("/api/views")
def create_view(body: ViewIn):
    name = (body.name or "").strip()[:MAX_NAME]
    if not name:
        raise HTTPException(400, "视图名称不能为空")
    if not body.tab:
        raise HTTPException(400, "缺少页签标识")
    conn = connect()
    try:
        views = _load(conn)
        if len(views) >= MAX_VIEWS:
            raise HTTPException(400, "视图数量已达上限（%d）" % MAX_VIEWS)
        item = {
            "id": str(int(time.time() * 1000)),
            "name": name,
            "tab": body.tab,
            "filters": _clean_filters(body.filters),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        views.append(item)
        _save(conn, views)
        return item
    finally:
        conn.close()


@router.put("/api/views/{view_id}")
def update_view(view_id: str, body: ViewUpdate):
    conn = connect()
    try:
        views = _load(conn)
        target = next((v for v in views if v.get("id") == view_id), None)
        if target is None:
            raise HTTPException(404, "视图不存在")
        if body.name is not None:
            name = body.name.strip()[:MAX_NAME]
            if not name:
                raise HTTPException(400, "视图名称不能为空")
            target["name"] = name
        if body.filters is not None:
            target["filters"] = _clean_filters(body.filters)
        _save(conn, views)
        return target
    finally:
        conn.close()


@router.delete("/api/views/{view_id}")
def delete_view(view_id: str):
    conn = connect()
    try:
        views = _load(conn)
        remaining = [v for v in views if v.get("id") != view_id]
        if len(remaining) == len(views):
            raise HTTPException(404, "视图不存在")
        _save(conn, remaining)
        return {"deleted": view_id}
    finally:
        conn.close()
