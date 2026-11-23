# -*- coding: utf-8 -*-
"""检索层：把查询词解析为候选 ``entry_id`` 列表。

v1.0 范围已纳入三项可选增强（2026-11-06 用户确认），它们的接入点集中在本模块：

* 扫描件 OCR：在 scanner 侧把识别文本写入 ``fts_entries.body``，本模块无需改动。
* 语义 / 向量检索：新增一个后端实现，与 FTS5 结果合并或重排后返回。
* ``SearchBackend`` 插件：新增 Backend 类，结果作为独立分组、不参与跨类型关联，
  默认关闭、仅在检测到已安装时启用。

M2 阶段保持单一 FTS5 + LIKE 双路径，不提前引入上述实现，避免超出本轮范围。
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("onlyone")


# 正文分片后同一个文件可能占多行，SQL 先多取再按 entry_id 去重，避免一个文件挤占全部名额。
_FETCH_ROWS = 900
_MAX_RESULTS = 300


def _dedupe(ids) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for eid in ids:
        if eid in seen:
            continue
        seen.add(eid)
        out.append(eid)
        if len(out) >= _MAX_RESULTS:
            break
    return out


def search_entries(conn: sqlite3.Connection, q: str) -> tuple[str, list[int]]:
    """返回 ``(mode, entry_ids)``。

    FTS5 trigram 对两字中文词返回 0（本机实测），因此：
    >=3 字符走 trigram；短词或 trigram 零命中时降级为 title/body/tags 的 LIKE 扫描。
    一个条目可能有多条分片行，结果按 entry_id 去重（保留首次出现顺序）。
    """
    ids: list[int] = []
    if len(q) >= 3:
        phrase = '"' + q.replace('"', '""') + '"'
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT entry_id FROM fts_entries WHERE fts_entries MATCH ? LIMIT ?",
                (phrase, _FETCH_ROWS))]
        except Exception as exc:  # noqa: BLE001 - 查询语法异常时降级
            log.warning("FTS5 查询降级 %r: %s", q, exc)
    ids = _dedupe(ids)
    if ids:
        return "fts5_trigram", ids

    pat = "%" + q + "%"
    ids = _dedupe(
        r[0] for r in conn.execute(
            "SELECT entry_id FROM fts_entries "
            "WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? LIMIT ?",
            (pat, pat, pat, _FETCH_ROWS)))
    return "like_fallback", ids


def semantic_candidates(conn: sqlite3.Connection, q: str,
                        exclude_ids: set[int] | tuple[int, ...] = (),
                        limit: int = 20) -> list[tuple[int, float]]:
    """M6-2：语义向量候选，作为独立分组返回，不与字面结果混排。"""
    from . import semantic  # noqa: PLC0415 - 避免检索层与向量层的循环依赖

    return semantic.search(conn, q, exclude_ids=exclude_ids, limit=limit)


def plugin_candidates(q: str) -> list[dict]:
    """M6-3：外部索引结果，独立分组、默认关闭。"""
    from . import backends  # noqa: PLC0415

    return backends.search_plugins(q)
