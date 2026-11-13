# -*- coding: utf-8 -*-
"""文件系统监听（M5-1）：watchdog 防抖 + 路径级增量 + 启动轻量校准。

默认只由 ``run.py`` 在生产形态（非 --no-window）启动；回归脚本用 --no-window，
不会启动监听，避免与脚本自己的扫描抢同一个 SQLite 库。
可用环境变量 ``ONLYONE_WATCHER=1`` 在 --no-window 下显式启用（供 verify_m5.py 测试）。

* 运行时：归集具体变化路径，防抖后走 ``scanner.scan_paths``，不再整 root 重扫；
* 启动 / 目录级事件 / 事件风暴超过阈值：走 ``scanner.calibrate_root`` 轻量校准
  （首次建索引才会完整 ``scan_root``）；
* 失败抽取队列每 24 小时最多自动重试一次，其余由前端失败清单手动重试。

所有入口共用 ``scanner._SCAN_LOCK`` 串行闸门，并复用 WPS COM 会话（M5-7）。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, DirMovedEvent,
    FileMovedEvent, FileSystemEventHandler,
)
from watchdog.observers import Observer

from .db import connect
from .scanner import SKIP_EXTS, calibrate_root, scan_paths

log = logging.getLogger("onlyone")

DEBOUNCE_SEC = 1.2
# 单个防抖窗口内变化路径过多（批量解压 / 复制）时回退轻量校准，
# 避免事件风暴把路径集合撑大、也避免 ReadDirectoryChangesW 缓冲溢出后漏事件。
MAX_PENDING_PATHS = 200
_lock = threading.Lock()
_refresh_lock = threading.Lock()
_pending: dict[int, set[str]] = {}
_full_calibrate: set[int] = set()
_state = {
    "enabled": False,
    "observer": None,
    "timers": {},
    "watches": {},
    "scans": 0,
    "last_event_at": None,
    "last_error": "",
}


class _Handler(FileSystemEventHandler):
    """按 root 归集具体变化路径；目录级事件与突发批量回退轻量校准。"""

    def __init__(self, root_id: int):
        super().__init__()
        self.root_id = root_id

    def on_any_event(self, event):
        name = Path(event.src_path).name
        if name.startswith("~$") or Path(name).suffix.lower() in SKIP_EXTS:
            return
        # 目录新建 / 删除 / 移动会改变整棵子树的 rel_path，单路径增量无法表达
        if isinstance(event, (DirCreatedEvent, DirDeletedEvent, DirMovedEvent)):
            _mark_full_calibrate(self.root_id)
            return
        if isinstance(event, DirModifiedEvent):
            return
        _schedule_path(self.root_id, event.src_path)
        if isinstance(event, FileMovedEvent):
            _schedule_path(self.root_id, event.dest_path)


def _reset_timer(root_id: int) -> None:
    """重置防抖定时器；调用方必须已持有 _lock。"""
    previous = _state["timers"].get(root_id)
    if previous:
        previous.cancel()
    timer = threading.Timer(DEBOUNCE_SEC, _run_pending, args=(root_id,))
    timer.daemon = True
    _state["timers"][root_id] = timer
    timer.start()


def _mark_full_calibrate(root_id: int) -> None:
    with _lock:
        if not _state["enabled"]:
            return
        _full_calibrate.add(root_id)
        _pending[root_id] = set()
        _state["last_event_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _reset_timer(root_id)


def _schedule_path(root_id: int, path: str) -> None:
    with _lock:
        if not _state["enabled"]:
            return
        if root_id not in _full_calibrate:
            bucket = _pending.setdefault(root_id, set())
            bucket.add(str(path))
            if len(bucket) > MAX_PENDING_PATHS:
                _full_calibrate.add(root_id)
                _pending[root_id] = set()
        _state["last_event_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _reset_timer(root_id)


def _run_pending(root_id: int) -> None:
    with _lock:
        full = root_id in _full_calibrate
        paths = _pending.pop(root_id, set())
        _full_calibrate.discard(root_id)
    if not full and not paths:
        return
    conn = connect()
    try:
        stats = (calibrate_root(conn, root_id) if full
                 else scan_paths(conn, root_id, paths))
        with _lock:
            _state["scans"] += 1
            _state["last_error"] = ""
        log.info("watcher 增量扫描 root=%s %s", root_id, stats)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state["last_error"] = str(exc)
        log.warning("watcher 增量扫描失败 root=%s: %s", root_id, exc)
    finally:
        conn.close()


def refresh() -> None:
    """根目录增删后重新调度监听；watcher 未启用时为空操作。"""
    with _refresh_lock, _lock:
        if not _state["enabled"]:
            return
        observer = _state["observer"]
    if observer is None:
        return

    conn = connect()
    try:
        roots = conn.execute("SELECT id, path, enabled FROM roots").fetchall()
    finally:
        conn.close()

    observer.unschedule_all()
    watches: dict[int, str] = {}
    for row in roots:
        if not row["enabled"]:
            continue
        path = Path(row["path"])
        if not path.is_dir():
            continue
        if path.parent == path:  # 盘符根目录：禁止递归监听整盘
            log.warning("跳过盘符根目录（不监听整盘）：%s", path)
            continue
        try:
            observer.schedule(_Handler(row["id"]), str(path), recursive=True)
            watches[row["id"]] = str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("watcher 无法监听 %s: %s", path, exc)
    with _lock:
        _state["watches"] = watches


def _calibrate() -> None:
    """启动时轻量校准：首次全量，此后只处理关闭期间的新增 / 变化 / 删除。"""
    conn = connect()
    try:
        rows = conn.execute("SELECT id, path FROM roots WHERE enabled=1").fetchall()
        ids = []
        for row in rows:
            path = Path(row["path"])
            if not path.is_dir() or path.parent == path:
                log.warning("校准跳过无效 / 盘符根目录：%s", path)
                continue
            ids.append(row["id"])
    finally:
        conn.close()
    for root_id in ids:
        cal_conn = connect()
        try:
            stats = calibrate_root(cal_conn, root_id)
            log.info("启动校准 root=%s %s", root_id, stats)
        except Exception as exc:  # noqa: BLE001
            log.warning("启动校准失败 root=%s: %s", root_id, exc)
        finally:
            cal_conn.close()


def start_watcher() -> None:
    with _lock:
        if _state["enabled"]:
            return
        observer = Observer()
        observer.daemon = True
        observer.start()
        _state["observer"] = observer
        _state["enabled"] = True
    log.info("watcher 已启动（防抖 %.1fs）", DEBOUNCE_SEC)
    refresh()
    threading.Thread(target=_calibrate, daemon=True, name="onlyone-calibrate").start()


def stop_watcher() -> None:
    with _lock:
        if not _state["enabled"]:
            return
        observer = _state["observer"]
        for timer in _state["timers"].values():
            timer.cancel()
        _state["timers"].clear()
        _pending.clear()
        _full_calibrate.clear()
        _state["enabled"] = False
        _state["observer"] = None
    if observer is not None:
        try:
            observer.stop()
            observer.join(timeout=3)
        except Exception:  # noqa: BLE001
            pass


def status() -> dict:
    with _lock:
        return {
            "enabled": _state["enabled"],
            "watched_roots": len(_state["watches"]),
            "scans": _state["scans"],
            "last_event_at": _state["last_event_at"],
            "last_error": _state["last_error"],
            "pending_paths": sum(len(v) for v in _pending.values()),
            "pending_full_calibrate": list(_full_calibrate),
        }
