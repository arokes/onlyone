# -*- coding: utf-8 -*-
"""重复文件报告（2026-11-25，功能 3）。

数据来源是 `scanner` 已经算好的 `files.sha256`：增量扫描时对 ≤50 MB 的文件算一次内容哈希，
所以这里只做聚合，不重新读磁盘。**只报告，不删除** —— 删除权始终留给用户，
接口只返回路径供资源管理器定位。

口径
----
* 同一 `sha256` 且行数 > 1 即为一组重复；
* 「可释放」= 组内总占用 - 单个大小（即保留一份后能省下的空间），不是「建议删掉多少」；
* `sha256=''` 的行是超过 50 MB 或读取失败的文件，未参与比对，在 `totals.unhashed` 里如实报数。
"""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter

from .db import connect

log = logging.getLogger("onlyone")
router = APIRouter()

MAX_GROUPS = 500
MAX_PATHS_PER_GROUP = 8


def _totals(conn: sqlite3.Connection, min_bytes: int) -> dict:
    row = conn.execute(
        """SELECT count(*)                                   AS total_files,
                  sum(CASE WHEN sha256<>'' THEN 1 ELSE 0 END) AS hashed_files,
                  sum(CASE WHEN sha256=''  THEN 1 ELSE 0 END) AS unhashed_files
           FROM files"""
    ).fetchone()
    group = conn.execute(
        """SELECT count(*) AS groups,
                  coalesce(sum(cnt - 1), 0)          AS redundant_files,
                  coalesce(sum(total - size), 0)     AS wasted_bytes
           FROM (SELECT sha256, count(*) AS cnt, sum(size) AS total, min(size) AS size
                 FROM files
                 WHERE sha256<>'' AND size >= ?
                 GROUP BY sha256 HAVING count(*) > 1)""",
        (min_bytes,),
    ).fetchone()
    return {
        "total_files": int(row["total_files"] or 0),
        "hashed_files": int(row["hashed_files"] or 0),
        "unhashed_files": int(row["unhashed_files"] or 0),
        "groups": int(group["groups"] or 0),
        "redundant_files": int(group["redundant_files"] or 0),
        "wasted_bytes": int(group["wasted_bytes"] or 0),
    }


@router.get("/api/duplicates")
def list_duplicates(min_mb: int = 0, limit: int = MAX_GROUPS):
    """按内容哈希列出重复文件组，按「可释放空间」降序。"""
    min_mb = max(0, min(int(min_mb), 10240))
    limit = max(1, min(int(limit), MAX_GROUPS))
    min_bytes = min_mb * 1048576

    conn = connect()
    try:
        groups = conn.execute(
            """SELECT sha256, count(*) AS count, min(size) AS size,
                      sum(size) AS total
               FROM files
               WHERE sha256<>'' AND size >= ?
               GROUP BY sha256 HAVING count(*) > 1
               ORDER BY (count(*) - 1) * min(size) DESC
               LIMIT ?""",
            (min_bytes, limit),
        ).fetchall()

        items = []
        for group in groups:
            paths = conn.execute(
                """SELECT rel_path, abs_path, mtime, root_id
                   FROM files WHERE sha256=?
                   ORDER BY mtime DESC LIMIT ?""",
                (group["sha256"], MAX_PATHS_PER_GROUP),
            ).fetchall()
            items.append({
                "sha256": group["sha256"],
                "size": int(group["size"] or 0),
                "count": int(group["count"] or 0),
                "wasted": int((group["total"] or 0) - (group["size"] or 0)),
                "paths": [{
                    "rel_path": p["rel_path"],
                    "abs_path": p["abs_path"],
                    "mtime": p["mtime"],
                } for p in paths],
            })

        return {"totals": _totals(conn, min_bytes), "groups": items}
    finally:
        conn.close()
