# -*- coding: utf-8 -*-
"""概览页聚合接口（2026-11-25）：一次读全，供「概览」首屏渲染。

设计见 ``docs/概览页设计方案.md``。三条硬约定：

* **单连接、少量查询、不做缓存**——概览必须反映刚保存的数据，缓存收益不值一致性风险；
* **任一区块失败只降级该块并记 warning，整体仍 200**——概览不能因为某张表出问题就整页报错；
* **超期判定只在这里做一次**——前端直接用 ``days_open`` / ``overdue``，不再重算，避免两套口径。
  阈值比较用 ``>`` 而不是 ``>=``：恰好 7 天不算超期，否则当天创建第二天就变红。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter

from . import maintenance, rich
from .db import DATA_DIR, connect
from .scanner import progress_snapshot
from .watcher import status as watcher_status

log = logging.getLogger("onlyone")
router = APIRouter()

DEFAULT_OVERDUE_DAYS = 7
DEFAULT_LIMIT = 8
# open_incidents 的返回上限：只影响列表条数，计数永远按全量算，
# 避免「未关闭 800 条时概览只数出 50」这种口径错位。
MAX_LISTED_INCIDENTS = 50


def _safe(warnings: list[str], label: str, fn: Callable[[], Any], fallback: Any) -> Any:
    """单块降级：异常写进 warnings 并返回 fallback，绝不让概览整体 500。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - 概览页要能容忍任何单块故障
        log.warning("overview.%s 失败：%s", label, exc)
        warnings.append("%s: %s" % (label, exc))
        return fallback


def _fmt_mtime(value: Any) -> str:
    """files.mtime 是 epoch 秒（REAL）；转本地时间串，前端不再处理时区。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _days_since(raw: Any, now: datetime) -> int | None:
    """按 occurred_at 算挂起天数；格式不支持时返回 None（不参与超期计数）。

    只取前 16 位，兼容库里 ``YYYY-MM-DD HH:MM`` 与带秒两种历史写法。
    """
    text = str(raw or "").strip()[:16]
    if len(text) < 16:
        return None
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (now - moment).days


def _excerpt(value: Any, size: int = 60) -> str:
    """富文本列 → 单行纯文本摘录；图片节点不产生字符（rich.extract_text 已保证）。"""
    return " ".join(rich.extract_text(value).split())[:size]


def _counts(conn: sqlite3.Connection) -> dict:
    """一条 SQL 用子查询取齐静态计数，避免 7 次往返。

    ``incidents_open`` 也在这里查：它只依赖 status 列，即使 ``_open_incidents``
    因数据问题降级，概览上的「未关闭 N 条」仍然是真实值。
    ``incidents_overdue`` 必须依赖天数阈值，统一在 Python 侧算。
    """
    row = conn.execute(
        """SELECT
             (SELECT count(*) FROM roots)                              AS roots,
             (SELECT count(*) FROM files)                              AS files,
             (SELECT count(*) FROM entries)                            AS entries,
             (SELECT count(*) FROM notes)                              AS notes,
             (SELECT count(*) FROM projects)                           AS projects,
             (SELECT count(*) FROM links)                              AS links,
             (SELECT count(*) FROM incidents WHERE status='open')      AS incidents_open,
             (SELECT count(*) FROM extract_failures WHERE attempts>=3) AS failures
        """
    ).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def _recent_files(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT f.rel_path, f.ext, f.size, f.mtime,
                  e.id AS entry_id, e.title, r.kind AS root_kind
           FROM files f
           LEFT JOIN entries e ON e.type='file' AND e.ref_id=f.id
           LEFT JOIN roots  r ON r.id = f.root_id
           ORDER BY f.mtime DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["mtime"] = _fmt_mtime(item.get("mtime"))
        # 索引行还在、entries 行被清掉时 title 为空：退回文件名，避免界面出现空行
        item["title"] = item.get("title") or str(
            item.get("rel_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        items.append(item)
    return items


def _open_incidents(conn: sqlite3.Connection, days: int) -> tuple[list[dict], int, int]:
    """返回 ``(列表明细, 未关闭总数, 超期条数)``。

    两个计数都按全量算，不受列表截断影响——否则「未关闭 800 条」会被概览数成 50。
    """
    rows = conn.execute(
        """SELECT id, occurred_at, system, category, status, symptom
           FROM incidents
           WHERE status='open'
           ORDER BY occurred_at ASC"""
    ).fetchall()
    now = datetime.now()
    items: list[dict] = []
    overdue = 0
    for row in rows:
        item = dict(row)
        symptom = _excerpt(item.get("symptom"))
        span = _days_since(item.get("occurred_at"), now)
        item["days_open"] = span
        item["overdue"] = span is not None and span > days
        item["symptom"] = symptom
        # 标题口径与 entries._title 对齐：系统 · 现象，缺省有兜底
        item["title"] = "%s · %s" % (
            str(item.get("system") or "").strip() or "未分类系统",
            symptom[:30] or "运维记录",
        )
        if item["overdue"]:
            overdue += 1
        items.append(item)
    return items[:MAX_LISTED_INCIDENTS], len(items), overdue


def _recent_notes(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT n.id, n.title, s.name AS section, n.updated_at
           FROM notes n
           JOIN sections s ON s.id = n.section_id
           WHERE n.is_private = 0
           ORDER BY n.updated_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_backup() -> tuple[str | None, int | None]:
    """最近一次全库备份。只认 ``onlyone-<时间戳>.db``，排除 compact 前的自动快照。"""
    folder = DATA_DIR / "backups"
    try:
        candidates = [p for p in folder.glob("onlyone-[0-9]*.db") if p.is_file()]
        if not candidates:
            return None, None
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        stamp = datetime.fromtimestamp(newest.stat().st_mtime)
    except OSError:
        return None, None
    return stamp.strftime("%Y-%m-%d %H:%M:%S"), (datetime.now() - stamp).days


def _storage(conn: sqlite3.Connection) -> dict:
    # include_counts=False：概览只要体积，跳过 fts/语义表的大表扫描（实测 160ms+）
    stats = maintenance.stats(conn, include_counts=False)
    backup_at, backup_days = _latest_backup()
    return {
        "db_mb": stats.get("file_mb", 0.0),
        "used_mb": stats.get("used_mb", 0.0),
        "free_mb": stats.get("free_mb", 0.0),
        "free_ratio": stats.get("free_ratio", 0.0),
        "backup_at": backup_at,
        "backup_days_ago": backup_days,
    }


def _scan() -> dict:
    snap = progress_snapshot()
    try:
        watch = watcher_status()
    except Exception as exc:  # noqa: BLE001 - 监听未启动不影响扫描状态展示
        log.debug("watcher.status 不可用：%s", exc)
        watch = {}
    return {
        "phase": snap.get("phase"),
        "percent": snap.get("percent"),
        "watched_roots": watch.get("watched_roots", 0),
        "last_event_at": watch.get("last_event_at"),
    }


@router.get("/api/overview")
def overview(days: int = DEFAULT_OVERDUE_DAYS, limit: int = DEFAULT_LIMIT):
    """概览页一次性聚合。单块失败降级为空值 + warnings，HTTP 始终 200。

    ``days`` / ``limit`` 越界时按边界夹取而不是返回 422：概览是只读展示，
    前端传错一个数不该让整页崩掉（设计稿 §5.1 已按此口径写断言）。
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 20))
    warnings: list[str] = []
    conn = connect()
    try:
        counts = _safe(warnings, "counts", lambda: _counts(conn), {})
        recent_files = _safe(warnings, "recent_files",
                             lambda: _recent_files(conn, limit), [])
        open_incidents, open_total, overdue = _safe(
            warnings, "open_incidents", lambda: _open_incidents(conn, days), ([], 0, 0))
        recent_notes = _safe(warnings, "recent_notes",
                             lambda: _recent_notes(conn, limit), [])
        storage = _safe(warnings, "storage", lambda: _storage(conn), None)
    finally:
        conn.close()

    counts = dict(counts)
    # 列表查询成功时以它的全量计数为准（两者理论一致）；失败时保留 SQL 的真实总数，
    # 避免降级把「未关闭 3 条」显示成 0。
    if open_total:
        counts["incidents_open"] = open_total
    counts.setdefault("incidents_open", 0)
    counts["incidents_overdue"] = overdue

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"days": days, "limit": limit},
        "counts": counts,
        "recent_files": recent_files,
        "open_incidents": open_incidents,
        "recent_notes": recent_notes,
        "storage": storage,
        "scan": _safe(warnings, "scan", _scan, None),
        "warnings": warnings,
    }
