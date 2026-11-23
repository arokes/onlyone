# -*- coding: utf-8 -*-
"""目录扫描（M2 正文索引；M5-1 失败重试队列；M5-7 WPS 会话复用；M8 进度上报）。

增量判定：size + mtime 不变则跳过；变更才算 sha256（大文件跳过）。
``force=True`` 用于重建索引；``extract_failures`` 中 attempts < 3 的记录会被自动重试，
达到 3 次即列入前端「失败清单」，需手动重试。
硬约束：疑似含凭据的文件完全不进 FTS（连条目都不建），密码不得落进索引。

M8 两处关键改动：

* **进度**：扫描前先枚举文件得到 total，处理过程中按文件更新 ``_PROGRESS``，
  前端可轮询 ``GET /api/scan/status`` 显示百分比与预计剩余时间。
  进度只存在进程内存里，不落库（重启后 old 值无意义）。
* **分块提交**：每 50 个文件或每 2 秒 commit 一次。原先整批扫描只在结尾提交，
  一次 10 分钟的全量扫描会把写锁占满 10 分钟，期间「新增项目/凭据/笔记」一律
  在 sqlite timeout 后报 ``database is locked``（500）。分块后持锁窗口降到秒级。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import semantic
from .extractor import SUPPORTED_EXTS, chunk_text, extract, wps_session

log = logging.getLogger("onlyone")

# P5：系统 / 系统级目录永不索引。曾出现把 D:\ 登记为根后扫进回收站（$RECYCLE.BIN）
# 与 Windows 目录，既慢又毫无检索价值，因此在目录层直接剪枝。
SKIP_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", "data", "dist", "build", ".venv",
    "$recycle.bin", "system volume information", "windows", "windows.old",
    "program files", "program files (x86)", "programdata", "recovery",
    "$winreagent", "$sysreset", "config.msi", "perflogs", "appdata",
}
# Windows 目录名大小写不固定，统一用小写比对；``.`` 开头的临时目录另判。
_SKIP_DIRS_LOWER = frozenset(d.lower() for d in SKIP_DIRS)
SKIP_EXTS = {".tmp", ".log", ".db", ".db-wal", ".db-shm", ".pyc", ".lock", ".part"}
MAX_HASH_BYTES = 50 * 1024 * 1024
MAX_ATTEMPTS = 3

# 分块提交阈值：先到者触发。50 个文件在含 OCR / WPS 转换的目录里通常是几秒。
COMMIT_EVERY_FILES = 50
COMMIT_EVERY_SECONDS = 2.0

# SQLite 单写者：HTTP 手动扫描、watcher 事件扫描、启动校准必须串行，否则会 database is locked。
_SCAN_LOCK = threading.Lock()

# v1.0 §2 硬约束 3：含密码的文档不得进 FTS。按文件名/相对路径保守排除，
# 命中即只登记 files 行、不创建 entries/fts_entries。
SENSITIVE_PAT = re.compile(
    r"(密码|口令|凭据|密钥|私钥|证书|账号清单|password|passwd|pwd|credential|secret|private[_-]?key)",
    re.I,
)

# ---------------------------------------------------------------- 进度状态（M8）

_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict = {
    "phase": "idle",           # idle / queued / scanning / done / error
    "root_id": None,
    "root_path": "",
    "done": 0,
    "total": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "stats": {},
    "error": "",
    "queued": [],              # 等待 _SCAN_LOCK 的任务：[{root_id, root_path}]
}


def _progress_reset(root_id: int | None, path: str = "", total: int = 0) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update({
            "phase": "scanning", "root_id": root_id, "root_path": path,
            "done": 0, "total": total, "current": "",
            "started_at": time.time(), "finished_at": None,
            "stats": {}, "error": "",
        })


def _progress_update(**kw) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update(kw)


def _progress_enqueue(root_id: int | None, path: str) -> None:
    """拿不到锁时登记排队，前端据此提示「另一个扫描正在进行」。"""
    with _PROGRESS_LOCK:
        if _PROGRESS["phase"] in ("scanning", "queued"):
            _PROGRESS["queued"] = [
                q for q in _PROGRESS["queued"] if q.get("root_id") != root_id
            ] + [{"root_id": root_id, "root_path": path}]


def _progress_dequeue(root_id: int | None) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS["queued"] = [
            q for q in _PROGRESS["queued"] if q.get("root_id") != root_id
        ]


def progress_snapshot() -> dict:
    """当前扫描进度快照（前端轮询用）。eta 为秒，无法估算时为 None。"""
    with _PROGRESS_LOCK:
        data = dict(_PROGRESS)
        data["queued"] = list(_PROGRESS["queued"])
    done, total = data["done"], data["total"]
    data["percent"] = round(done * 100.0 / total, 1) if total else 0.0
    started = data.get("started_at")
    if data["phase"] == "scanning" and started and done > 0:
        elapsed = time.time() - started
        data["elapsed"] = round(elapsed, 1)
        data["eta"] = round(elapsed / done * max(total - done, 0), 1)
    else:
        data["elapsed"] = round(time.time() - started, 1) if started else 0
        data["eta"] = None
    return data


# ---------------------------------------------------------------- 索引写入


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sync_entry(conn, file_id: int, name: str, body: str = "") -> int:
    """写入/更新统一条目，并把 title + 正文写入 FTS（凭据文件不会调用本函数）。

    正文按 ``chunk_text`` 分片成多行（每条 ≤ CHUNK_CHARS，共用同一 entry_id）：
    单个文件因此不再受单条正文上限限制，检索侧按 entry_id 去重并汇总命中次数。
    """
    summary = " ".join((body or "").split())[:200]
    row = conn.execute(
        "SELECT id FROM entries WHERE type='file' AND ref_id=?", (file_id,)
    ).fetchone()
    if row:
        eid = row["id"]
        conn.execute(
            "UPDATE entries SET title=?, summary=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (name, summary, eid),
        )
    else:
        eid = conn.execute(
            "INSERT INTO entries(type,ref_id,title,summary) VALUES('file',?,?,?)",
            (file_id, name, summary),
        ).lastrowid
    conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (eid,))
    chunks = chunk_text(body or name) or [name]
    conn.executemany(
        "INSERT INTO fts_entries(entry_id,title,body,tags) VALUES(?,?,?,'')",
        [(eid, name, chunk) for chunk in chunks],
    )
    semantic.sync_entry(conn, eid, name, body)
    return eid


def _drop_entry(conn, file_id: int) -> None:
    row = conn.execute(
        "SELECT id FROM entries WHERE type='file' AND ref_id=?", (file_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (row["id"],))
        semantic.drop_entry(conn, row["id"])
        conn.execute("DELETE FROM entries WHERE id=?", (row["id"],))


def _record_failure(conn, file_id: int, error: str) -> int:
    """累加失败次数，返回累加后的 attempts。"""
    row = conn.execute("SELECT attempts FROM extract_failures WHERE file_id=?",
                       (file_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    conn.execute(
        """INSERT INTO extract_failures(file_id,attempts,last_error,updated_at)
           VALUES(?,?,?,datetime('now','localtime'))
           ON CONFLICT(file_id) DO UPDATE SET
             attempts=excluded.attempts, last_error=excluded.last_error,
             updated_at=excluded.updated_at""",
        (file_id, attempts, error[:500]),
    )
    return attempts


def _extract_one(full: Path, rel: str, name: str, digest: str, stats: dict):
    """返回 (extract_status, extract_error, body)。失败时 body 退回文件名。"""
    if SENSITIVE_PAT.search(name) or SENSITIVE_PAT.search(rel):
        # M4-3 复核结论：维持保守策略（完全不建条目），但计入统计让用户在索引目录页可感知。
        stats["sensitive"] += 1
        return "skipped_sensitive", "", ""
    if not digest:  # >50MB，仅索引文件名
        return "skipped_large", "", name
    if full.suffix.lower() not in SUPPORTED_EXTS:
        return "skipped_type", "", name

    stats["extracted"] += 1
    result = extract(full)
    if result.error:
        stats["extract_failed"] += 1
        log.warning("抽取失败 %s: %s", full, result.error[:200])
        return "failed", result.error[:500], name
    return "ok", "", result.body or name


def _walk_files(base: Path) -> list[tuple[Path, str]]:
    """枚举待处理文件（一遍目录遍历），用于算 total 与后续删除判定。"""
    items: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in _SKIP_DIRS_LOWER and not d.startswith(".")
        ]
        for name in filenames:
            if name.startswith("~$") or Path(name).suffix.lower() in SKIP_EXTS:
                continue
            full = Path(dirpath) / name
            try:
                rel = str(full.relative_to(base))
            except ValueError:  # 符号链接指到根目录外
                continue
            items.append((full, rel))
    return items


# ---------------------------------------------------------------- 扫描入口


def scan_root(conn, root_id: int, *, force: bool = False,
              retry_failed: bool = True) -> dict:
    """扫描入口：串行化 + WPS 实例会话复用（M5-7）+ 进度上报（M8）。

    ``_SCAN_LOCK`` 是进程级串行闸门：watcher 增量、启动校准、手动扫描共用。
    拿不到锁时先登记排队（前端提示「另一个扫描正在进行」），再阻塞等待。
    """
    if not _SCAN_LOCK.acquire(blocking=False):
        row = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
        _progress_enqueue(root_id, str(row["path"]) if row else "")
        _SCAN_LOCK.acquire()
    _progress_dequeue(root_id)
    try:
        with wps_session():
            return _run(conn, root_id, force=force, retry_failed=retry_failed)
    finally:
        _SCAN_LOCK.release()


def _run(conn, root_id: int, *, force: bool, retry_failed: bool) -> dict:
    row = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    path = str(row["path"]) if row else ""
    _progress_reset(root_id, path, total=0)
    try:
        stats = _scan_root_impl(conn, root_id, force=force, retry_failed=retry_failed)
    except Exception as exc:  # noqa: BLE001 - 进度必须收尾，异常照常抛给调用方
        _progress_update(phase="error", error=str(exc), finished_at=time.time())
        raise
    _progress_update(phase="done", finished_at=time.time(), current="", stats=dict(stats))
    return stats


def _scan_root_impl(conn, root_id: int, *, force: bool, retry_failed: bool) -> dict:
    root = conn.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
    if root is None:
        raise KeyError("索引根目录不存在: %s" % root_id)
    base = Path(root["path"])
    if not base.is_dir():
        raise NotADirectoryError(str(base))

    t0 = time.time()
    stats = {"root_id": root_id, "path": str(base), "scanned": 0,
             "added": 0, "updated": 0, "removed": 0, "failed": 0,
             "extracted": 0, "extract_failed": 0, "sensitive": 0,
             "retried": 0, "given_up": 0, "seconds": 0.0}

    # M8：先枚举再处理，才能给出可信的 total / 百分比。
    pending = _walk_files(base)
    total = len(pending)
    seen = {rel for _, rel in pending}
    stats["scanned"] = total
    _progress_update(total=total, done=0, current="")

    # M5-1：历史 failed 文件即使 size+mtime 未变化也要重试；达到 3 次则只在失败清单里等手动重试。
    retry_ids: set[int] = set()
    if retry_failed:
        retry_ids = {r[0] for r in conn.execute(
            """SELECT ef.file_id FROM extract_failures ef
               JOIN files f ON f.id = ef.file_id
               WHERE f.root_id = ? AND ef.attempts < ?""",
            (root_id, MAX_ATTEMPTS))}

    last_commit = time.time()
    for idx, (full, rel) in enumerate(pending, 1):
        name = full.name
        try:
            st = full.stat()
        except OSError as exc:
            stats["failed"] += 1
            log.warning("stat 失败 %s: %s", full, exc)
            _progress_update(done=idx)
            continue

        old = conn.execute(
            "SELECT * FROM files WHERE root_id=? AND rel_path=?", (root_id, rel)
        ).fetchone()
        is_retry = bool(old) and old["id"] in retry_ids
        if (old and not force and not is_retry
                and old["size"] == st.st_size
                and abs(old["mtime"] - st.st_mtime) < 0.001):
            _progress_update(done=idx)
            continue

        digest = _sha256(full) if st.st_size <= MAX_HASH_BYTES else ""
        conn.execute(
            """INSERT INTO files(root_id,rel_path,abs_path,ext,size,mtime,sha256,
                                 extract_status,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))
               ON CONFLICT(root_id,rel_path) DO UPDATE SET
                 abs_path=excluded.abs_path, ext=excluded.ext, size=excluded.size,
                 mtime=excluded.mtime, sha256=excluded.sha256,
                 extract_status='pending', extract_error='',
                 indexed_at=datetime('now','localtime')""",
            (root_id, rel, str(full), full.suffix.lower(), st.st_size, st.st_mtime, digest,
             "skipped_large" if not digest else "pending"),
        )
        fid = conn.execute(
            "SELECT id FROM files WHERE root_id=? AND rel_path=?", (root_id, rel)
        ).fetchone()["id"]

        status, error, body = _extract_one(full, rel, name, digest, stats)
        conn.execute("UPDATE files SET extract_status=?, extract_error=? WHERE id=?",
                     (status, error, fid))
        if status == "failed":
            attempts = _record_failure(conn, fid, error)
            if attempts >= MAX_ATTEMPTS:
                stats["given_up"] += 1
        else:
            conn.execute("DELETE FROM extract_failures WHERE file_id=?", (fid,))
        if is_retry:
            stats["retried"] += 1
        if status == "skipped_sensitive":
            _drop_entry(conn, fid)
        else:
            _sync_entry(conn, fid, name, body)
        stats["added" if old is None else "updated"] += 1

        # M8：分块提交，避免一次全量扫描把 SQLite 写锁占满整个扫描周期。
        if idx % COMMIT_EVERY_FILES == 0 or time.time() - last_commit > COMMIT_EVERY_SECONDS:
            conn.commit()
            last_commit = time.time()
        _progress_update(done=idx, current=rel, stats=dict(stats))

    for row in conn.execute("SELECT id, rel_path FROM files WHERE root_id=?", (root_id,)).fetchall():
        if row["rel_path"] not in seen:
            _drop_entry(conn, row["id"])
            conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
            stats["removed"] += 1

    conn.execute("UPDATE roots SET last_scan_at=datetime('now','localtime') WHERE id=?", (root_id,))
    conn.commit()
    stats["seconds"] = round(time.time() - t0, 3)
    log.info("扫描完成 root=%s %s", root_id, stats)
    return stats


# ---------------------------------------------------------------- 后台任务（M8）


_JOB_LOCK = threading.Lock()
_JOBS: dict[int, threading.Thread] = {}


def start_scan_job(root_id: int, *, force: bool = False) -> tuple[bool, str]:
    """提交后台扫描，立即返回。同一 root 已在队列/执行中时不重复提交。"""
    with _JOB_LOCK:
        existing = _JOBS.get(root_id)
        if existing is not None and existing.is_alive():
            return False, "该目录已在扫描队列中"
        thread = threading.Thread(
            target=_job_main, args=(root_id, force),
            daemon=True, name="onlyone-scan-%d" % root_id,
        )
        _JOBS[root_id] = thread
        thread.start()
    return True, ""


def _job_main(root_id: int, force: bool) -> None:
    from .db import connect

    conn = connect()
    try:
        scan_root(conn, root_id, force=force)
    except Exception as exc:  # noqa: BLE001 - 后台任务失败只记日志与进度，不影响进程
        log.warning("后台扫描失败 root=%s: %s", root_id, exc)
    finally:
        conn.close()
        # 先把自己从 _JOBS 摘掉再交给维护线程：``_JOBS`` 是否存活就是「该目录正在
        # 扫描」的判定，扫描线程若挂着 VACUUM 多活几秒，紧随其后的 rebuild 会吃 409
        # （verify_m8 实测踩到）。
        with _JOB_LOCK:
            if _JOBS.get(root_id) is threading.current_thread():
                _JOBS.pop(root_id, None)
        _spawn_maintenance()


_COMPACT_THREAD: threading.Thread | None = None


def _spawn_maintenance() -> None:
    """扫描结束后另起守护线程做索引维护，不阻塞也不占用扫描任务槽位。"""
    global _COMPACT_THREAD
    with _JOB_LOCK:
        if _COMPACT_THREAD is not None and _COMPACT_THREAD.is_alive():
            return
        if any(t.is_alive() for t in _JOBS.values()):
            # 还有别的目录在扫，让最后结束的那个任务来触发，避免抢写锁
            log.debug("其它目录仍在扫描，跳过本次索引维护")
            return
        thread = threading.Thread(target=_maintenance_main, daemon=True,
                                  name="onlyone-compact")
        _COMPACT_THREAD = thread
        thread.start()


def _maintenance_main() -> None:
    """做一次「够脏才动」的索引维护（方案 A）。

    刷新/重建条目是 ``DELETE`` + ``INSERT``，FTS5 只留 tombstone、库里留空闲页，
    不维护的话库文件会按 3 倍膨胀（实测 309 MB 里 201 MB 是垃圾）。
    门槛：空闲页 ≥16 MB 且距上次维护 ≥1 小时，见 maintenance.AUTO_*。
    """
    from .maintenance import auto_compact_if_needed

    def _no_scan_running() -> bool:
        return not any(t.is_alive() for t in _JOBS.values())

    try:
        res = auto_compact_if_needed(guard=_no_scan_running)
    except Exception as exc:  # noqa: BLE001 - 维护失败不得影响扫描结果
        log.debug("索引维护跳过：%s", exc)
        return
    if res and res.get("ok"):
        if res.get("vacuumed"):
            log.info("扫描后已压缩索引库：%.1f MB → %.1f MB（耗时 %.1fs）",
                     res["before"]["file_mb"], res["after"]["file_mb"],
                     res.get("cost_sec", 0))
        else:
            log.info("扫描后已合并索引段，VACUUM 留待下次：%s", res.get("reason", ""))
    elif res and res.get("reason"):
        log.info("索引维护未完成，可退出应用后跑 tools/db_maintenance.py：%s",
                 res["reason"])


# ---------------------------------------------------------------- A / B 增量扫描
#
# 全量扫描 ``scan_root`` 的契约与行为保持不变（回归脚本 verify_m5 / verify_m8 依赖它）。
# 以下新增「路径级」处理能力，供启动轻量校准（A）与 watchdog 事件增量（B）复用。

FAILURE_RETRY_KEY = "last_failure_retry_at"
FAILURE_RETRY_INTERVAL_SEC = 24 * 3600


def _new_stats(root_id: int, path: str = "") -> dict:
    return {"root_id": root_id, "path": path, "scanned": 0,
            "added": 0, "updated": 0, "removed": 0, "failed": 0,
            "extracted": 0, "extract_failed": 0, "sensitive": 0,
            "retried": 0, "given_up": 0, "seconds": 0.0}


def _process_path(conn, root_id: int, full: Path, rel: str, stats: dict,
                  *, retry: bool = False) -> None:
    """处理单个存在的文件：size+mtime 未变则跳过，否则重新抽取并同步索引。

    ``retry=True`` 用于失败队列的低频重试：忽略 size+mtime，强制再抽一次。
    """
    old = conn.execute(
        "SELECT * FROM files WHERE root_id=? AND rel_path=?", (root_id, rel)
    ).fetchone()
    try:
        st = full.stat()
    except OSError as exc:
        stats["failed"] += 1
        log.warning("stat 失败 %s: %s", full, exc)
        return
    if (old and not retry and old["size"] == st.st_size
            and abs(old["mtime"] - st.st_mtime) < 0.001):
        return

    digest = _sha256(full) if st.st_size <= MAX_HASH_BYTES else ""
    conn.execute(
        """INSERT INTO files(root_id,rel_path,abs_path,ext,size,mtime,sha256,
                             extract_status,indexed_at)
           VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))
           ON CONFLICT(root_id,rel_path) DO UPDATE SET
             abs_path=excluded.abs_path, ext=excluded.ext, size=excluded.size,
             mtime=excluded.mtime, sha256=excluded.sha256,
             extract_status='pending', extract_error='',
             indexed_at=datetime('now','localtime')""",
        (root_id, rel, str(full), full.suffix.lower(), st.st_size, st.st_mtime, digest,
         "skipped_large" if not digest else "pending"),
    )
    fid = conn.execute(
        "SELECT id FROM files WHERE root_id=? AND rel_path=?", (root_id, rel)
    ).fetchone()["id"]

    status, error, body = _extract_one(full, rel, full.name, digest, stats)
    conn.execute("UPDATE files SET extract_status=?, extract_error=? WHERE id=?",
                 (status, error, fid))
    if status == "failed":
        if _record_failure(conn, fid, error) >= MAX_ATTEMPTS:
            stats["given_up"] += 1
    else:
        conn.execute("DELETE FROM extract_failures WHERE file_id=?", (fid,))
    if retry:
        stats["retried"] += 1
    if status == "skipped_sensitive":
        _drop_entry(conn, fid)
    else:
        _sync_entry(conn, fid, full.name, body)
    stats["added" if old is None else "updated"] += 1


def _remove_path(conn, root_id: int, rel: str, stats: dict) -> None:
    """文件已不存在：移除 files 行、失败队列与检索条目。"""
    old = conn.execute(
        "SELECT id FROM files WHERE root_id=? AND rel_path=?", (root_id, rel)
    ).fetchone()
    if old:
        _drop_entry(conn, old["id"])
        conn.execute("DELETE FROM extract_failures WHERE file_id=?", (old["id"],))
        conn.execute("DELETE FROM files WHERE id=?", (old["id"],))
        stats["removed"] += 1


def _normalize_rel(base: Path, path) -> str | None:
    """把事件/校准传入的路径归一成相对 root 的 rel_path；被排除的路径返回 None。"""
    p = Path(path)
    try:
        rel = str(p.relative_to(base)) if p.is_absolute() else str(p)
    except ValueError:
        return None
    if not rel or rel == ".":
        return None
    if p.name.startswith("~$") or p.suffix.lower() in SKIP_EXTS:
        return None
    parents = list(Path(rel).parts[:-1])
    if any(part.lower() in _SKIP_DIRS_LOWER or part.startswith(".")
           for part in parents):
        return None
    return rel


def _run_paths(conn, root_id: int, paths, *, retry_failed: bool = False,
               update_last_scan: bool = False) -> dict:
    """不加锁的路径级处理实现；调用方负责持有 _SCAN_LOCK。"""
    row = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    if row is None:
        raise KeyError("索引根目录不存在: %s" % root_id)
    base = Path(str(row["path"]))
    t0 = time.time()
    stats = _new_stats(root_id, str(base))

    rels: list[str] = []
    seen_rel: set[str] = set()
    for item in paths:
        rel = _normalize_rel(base, item)
        if rel and rel not in seen_rel:
            seen_rel.add(rel)
            rels.append(rel)
    stats["scanned"] = len(rels)
    _progress_reset(root_id, str(base), total=len(rels))

    for idx, rel in enumerate(rels, 1):
        full = base / rel
        if full.exists():
            _process_path(conn, root_id, full, rel, stats, retry=retry_failed)
        else:
            _remove_path(conn, root_id, rel, stats)
        if idx % COMMIT_EVERY_FILES == 0:
            conn.commit()
        _progress_update(done=idx, current=rel, stats=dict(stats))

    if update_last_scan:
        conn.execute("UPDATE roots SET last_scan_at=datetime('now','localtime') WHERE id=?",
                     (root_id,))
    conn.commit()
    stats["seconds"] = round(time.time() - t0, 3)
    _progress_update(phase="done", finished_at=time.time(), current="",
                     stats=dict(stats))
    return stats


def _acquire_scan_lock(conn, root_id: int) -> None:
    """与 scan_root 同样的串行闸门 + 排队提示。"""
    if not _SCAN_LOCK.acquire(blocking=False):
        row = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
        _progress_enqueue(root_id, str(row["path"]) if row else "")
        _SCAN_LOCK.acquire()
    _progress_dequeue(root_id)


@contextmanager
def root_write_lock(conn, root_id: int):
    """供 API 层写操作（如移除索引根目录）与扫描共用同一把串行闸门。"""
    _acquire_scan_lock(conn, root_id)
    try:
        yield
    finally:
        _SCAN_LOCK.release()


def scan_paths(conn, root_id: int, paths) -> dict:
    """B：只处理给定路径（watchdog 事件增量入口）。"""
    _acquire_scan_lock(conn, root_id)
    try:
        with wps_session():
            return _run_paths(conn, root_id, paths)
    finally:
        _SCAN_LOCK.release()


def _should_retry_failures(conn) -> bool:
    """距上次失败重试是否满 24 小时；表不存在 / 值异常时保守返回 True。"""
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key=?",
                           (FAILURE_RETRY_KEY,)).fetchone()
    except Exception:  # noqa: BLE001 - 旧库尚未建表时退化到重试一次
        return True
    if row is None:
        return True
    try:
        last = datetime.strptime(str(row["value"]), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.now() - last).total_seconds() >= FAILURE_RETRY_INTERVAL_SEC


def _mark_failure_retry(conn) -> None:
    # 旧库可能尚未执行新的 init_db，这里兜底建表，避免校准整体失败。
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta "
                 "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """INSERT INTO app_meta(key,value) VALUES(?,datetime('now','localtime'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (FAILURE_RETRY_KEY,),
    )


def _calibrate_impl(conn, root_id: int) -> dict:
    """A：一次目录枚举 + 一次批量 DB 比对，只处理新增/变化/删除与到期失败重试。"""
    row = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    base = Path(str(row["path"]))
    t0 = time.time()
    stats = _new_stats(root_id, str(base))

    pending = _walk_files(base)
    stats["scanned"] = len(pending)
    db = {r["rel_path"]: (r["size"], r["mtime"]) for r in conn.execute(
        "SELECT rel_path,size,mtime FROM files WHERE root_id=?", (root_id,))}

    todo: dict[str, tuple[str, Path | None, bool]] = {}
    seen: set[str] = set()
    for full, rel in pending:
        seen.add(rel)
        try:
            st = full.stat()
        except OSError:
            continue
        cur = db.get(rel)
        # 与全量扫描保持同一判定（mtime 容差 0.001s），避免浮点表示差异造成重复抽取
        if (cur is None or cur[0] != st.st_size
                or abs(cur[1] - st.st_mtime) >= 0.001):
            todo[rel] = ("upsert", full, False)
    for rel in db:
        if rel not in seen:
            todo[rel] = ("remove", None, False)

    if _should_retry_failures(conn):
        for fr in conn.execute(
            """SELECT f.abs_path, f.rel_path FROM extract_failures ef
               JOIN files f ON f.id = ef.file_id
               WHERE f.root_id = ? AND ef.attempts < ?""",
            (root_id, MAX_ATTEMPTS)).fetchall():
            rel = str(fr["rel_path"])
            if rel in seen and rel not in todo:
                todo[rel] = ("upsert", Path(str(fr["abs_path"])), True)
        _mark_failure_retry(conn)

    _progress_reset(root_id, str(base), total=len(todo))
    last_commit = time.time()
    for idx, (rel, (kind, full, retry)) in enumerate(todo.items(), 1):
        if kind == "remove" or full is None or not full.exists():
            _remove_path(conn, root_id, rel, stats)
        else:
            _process_path(conn, root_id, full, rel, stats, retry=retry)
        if idx % COMMIT_EVERY_FILES == 0 or time.time() - last_commit > COMMIT_EVERY_SECONDS:
            conn.commit()
            last_commit = time.time()
        _progress_update(done=idx, current=rel, stats=dict(stats))

    conn.execute("UPDATE roots SET last_scan_at=datetime('now','localtime') WHERE id=?",
                 (root_id,))
    conn.commit()
    stats["seconds"] = round(time.time() - t0, 3)
    _progress_update(phase="done", finished_at=time.time(), current="",
                     stats=dict(stats))
    return stats


def calibrate_root(conn, root_id: int) -> dict:
    """A：启动校准 / B：批量事件超阈值时的回退入口。

    ``last_scan_at`` 为空（首次建索引）才走完整 ``scan_root``；此后只做轻量校准。
    """
    _acquire_scan_lock(conn, root_id)
    try:
        row = conn.execute("SELECT last_scan_at FROM roots WHERE id=?",
                           (root_id,)).fetchone()
        if row is None:
            raise KeyError("索引根目录不存在: %s" % root_id)
        with wps_session():
            if not row["last_scan_at"]:
                log.info("首次校准 root=%s，执行完整扫描", root_id)
                stats = _run(conn, root_id, force=False, retry_failed=False)
                # 全量扫描已经抽过所有文件（含失败项），24h 窗口从这次算起，
                # 避免紧接着的重启立刻再重试一遍坏文档。
                _mark_failure_retry(conn)
                conn.commit()
                return stats
            return _calibrate_impl(conn, root_id)
    finally:
        _SCAN_LOCK.release()
