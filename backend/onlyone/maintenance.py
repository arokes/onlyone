# -*- coding: utf-8 -*-
"""索引库维护：FTS5 段合并 + 空间回收（2026-11-17 方案 A）。

为什么需要它（本机实测数据，1367 条条目）：

* ``fts_entries`` 是**普通 fts5**（非 external content）。每次重建/更新条目都是
  ``DELETE`` + ``INSERT``，FTS5 只写 tombstone 段，**DELETE 不清理段数据**；
  反复重建后索引里 40% 以上是死段。实测原样 VACUUM 后 182.7 MB，
  执行 ``optimize`` 后降到 108.1 MB（比 ``rebuild`` 的 110.0 MB 更小且更快）。
* 每个条目更新都会留下空闲页。实测 freelist 32323 页 = 126.3 MB，
  只有 ``VACUUM`` 才能把页还给操作系统（``DELETE`` / ``optimize`` 都不行）。

所以两步都要做，且顺序固定：**先 optimize 压掉死段，再 VACUUM 归还空闲页**。
单独 VACUUM 只能到 182.7 MB，单独 optimize 也不释放 freelist。

两个入口：

* :func:`compact` —— 供扫描任务收尾自动调用；任何失败都只返回原因，不抛异常，
  绝不能让索引维护拖垮扫描或进程。
* :func:`stats` —— 体积/碎片指标，写日志与 CLI 输出共用。

``VACUUM`` 需要独占锁，且临时空间约等于库大小；应用正在扫描或写入时会拿不到锁，
此时返回 ``locked`` 原因，交给 ``tools/db_maintenance.py`` 在应用退出后执行。
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from .db import DB_PATH, connect

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# 自动维护门槛：空闲页超过该数量才值得 VACUUM（小库收益抵不上锁开销）。
# 4000 页 = 16 MB；可用 ONLYONE_COMPACT_MIN_FREE_PAGES 覆盖。
AUTO_MIN_FREE_PAGES = _env_int("ONLYONE_COMPACT_MIN_FREE_PAGES", 4000)
# 自动维护最小间隔，避免频繁扫描时反复 VACUUM；0 表示每次都做。
AUTO_MIN_INTERVAL_SEC = _env_int("ONLYONE_COMPACT_MIN_INTERVAL_SEC", 3600)


def stats(conn: sqlite3.Connection | None = None) -> dict:
    """返回体积与碎片指标（MB）。"""
    own = conn is None
    conn = conn or connect()
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        fts_rows = conn.execute(
            "SELECT count(DISTINCT entry_id) FROM fts_entries").fetchone()[0]
        fts_chunks = conn.execute("SELECT count(*) FROM fts_entries").fetchone()[0]
        semantic_rows = conn.execute("SELECT count(*) FROM semantic_index").fetchone()[0]
    finally:
        if own:
            conn.close()
    total = page_count * page_size
    free = free_pages * page_size
    return {
        "file_mb": round(_file_mb(DB_PATH), 2),
        "used_mb": round((total - free) / 1048576, 2),
        "free_mb": round(free / 1048576, 2),
        "free_pages": free_pages,
        "free_ratio": round(free / total, 3) if total else 0.0,
        "entries": fts_rows,
        "fts_chunks": fts_chunks,
        "semantic_rows": semantic_rows,
    }


def _file_mb(path: Path) -> float:
    try:
        return path.stat().st_size / 1048576
    except OSError:
        return 0.0


def _disk_ok(need_bytes: int) -> tuple[bool, str]:
    """VACUUM 需要约等于库大小的临时空间；不够就明确拒绝，别让它写到一半失败。"""
    try:
        usage = shutil.disk_usage(DB_PATH.parent)
    except OSError as exc:
        return False, "无法读取磁盘空间：%s" % exc
    if usage.free < need_bytes * 1.1:
        return False, ("磁盘剩余 %.0f MB，不足 VACUUM 所需 %.0f MB"
                       % (usage.free / 1048576, need_bytes * 1.1 / 1048576))
    return True, ""


def compact(*, vacuum: bool = True, timeout: float = 30.0,
            conn: sqlite3.Connection | None = None,
            guard: "Callable[[], bool] | None" = None) -> dict:
    """合并 FTS5 段并回收空闲页。

    参数
    ----
    vacuum : 是否执行 VACUUM（False 时只做 optimize，可在应用中安全调用）
    timeout: 拿锁等待秒数；应用繁忙时可给短值，拿到不到就跳过
    conn   : 复用已有连接（不传则自己开一个）
    guard  : VACUUM 前的二次确认；返回 False 则只保留 optimize 结果。
             自动维护用它避免「刚要 VACUUM，新扫描已经开跑」的抢锁。

    返回 ``{"ok": bool, "reason": str, "before": {...}, "after": {...}}``，
    永不抛异常。
    """
    started = time.time()
    own = conn is None
    conn = conn or connect()
    result: dict = {"ok": False, "reason": "", "vacuumed": False, "optimized": False}
    try:
        before = stats(conn)
        result["before"] = before

        if vacuum:
            need = int(before["file_mb"] * 1048576)
            ok, why = _disk_ok(need)
            if not ok:
                result["reason"] = why
                result["after"] = before
                return result

        # ① optimize：合并段、清 tombstone。只写索引，不阻塞读。
        try:
            conn.commit()
            conn.execute("INSERT INTO fts_entries(fts_entries) VALUES('optimize')")
            conn.commit()
            result["optimized"] = True
        except sqlite3.OperationalError as exc:
            result["reason"] = "optimize 失败：%s" % exc
            result["after"] = stats(conn)
            return result

        # ② VACUUM：把空闲页还给系统。必须先结束事务（VACUUM 不能在事务内执行）。
        if vacuum:
            if guard is not None and not guard():
                result["ok"] = True  # optimize 已完成，结果有效
                result["reason"] = "有扫描任务在跑，已跳过 VACUUM"
                result["after"] = stats(conn)
                result["cost_sec"] = round(time.time() - started, 2)
                return result
            try:
                conn.commit()
                conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
                conn.execute("VACUUM")
                result["vacuumed"] = True
            except sqlite3.OperationalError as exc:
                # 常见原因：应用正在扫描/写入，拿不到独占锁。不算错误，交给离线工具。
                result["reason"] = "VACUUM 未执行（%s）" % exc
                result["after"] = stats(conn)
                return result
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass  # 非 WAL 或仍有读者，忽略

        result["ok"] = True
        result["after"] = stats(conn)
        result["cost_sec"] = round(time.time() - started, 2)
        return result
    except Exception as exc:  # noqa: BLE001 - 维护失败绝不能影响调用方
        log.warning("索引维护失败：%s", exc)
        result["reason"] = "%s: %s" % (type(exc).__name__, exc)
        return result
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if result.get("optimized") or result.get("vacuumed"):
            b, a = result.get("before", {}), result.get("after", {})
            log.info("索引维护：%.1f MB → %.1f MB（opt=%s vacuum=%s，耗时 %.1fs%s）",
                     b.get("file_mb", 0), a.get("file_mb", 0),
                     result["optimized"], result["vacuumed"],
                     time.time() - started,
                     "；" + result["reason"] if result["reason"] else "")


# ---------------------------------------------------------------- 自动维护（扫描收尾）
_AUTO_LOCK = threading.Lock()


def auto_compact_if_needed(guard: Callable[[], bool] | None = None) -> dict | None:
    """扫描任务结束后调用：仅当空闲页够多、且距上次维护超过 1 小时才动手。

    ``guard`` 在 VACUUM 前再确认一次「当前没有别的扫描在跑」，防止刚要压缩时
    新任务已经开跑，抢着要独占锁。

    返回 None 表示「不需要 / 正在跑 / 刚跑过」；返回 dict 表示真的执行了。
    """
    if not _AUTO_LOCK.acquire(blocking=False):
        return None
    try:
        conn = connect()
        try:
            cur = stats(conn)
            if cur["free_pages"] < AUTO_MIN_FREE_PAGES:
                return None
            last = conn.execute(
                "SELECT value FROM app_meta WHERE key='last_compact_at'").fetchone()
            if last and last[0]:
                try:
                    if time.time() - float(last[0]) < AUTO_MIN_INTERVAL_SEC:
                        return None
                except ValueError:
                    pass
        finally:
            conn.close()

        res = compact(vacuum=True, timeout=15, guard=guard)
        # 只有真的 VACUUM 成功才记时间戳：被 guard 跳过或拿不到锁时要允许下次重试。
        if res.get("vacuumed"):
            conn = connect()
            try:
                conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('last_compact_at',?)",
                             (str(time.time()),))
                conn.commit()
            finally:
                conn.close()
        return res
    finally:
        _AUTO_LOCK.release()
