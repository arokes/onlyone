# -*- coding: utf-8 -*-
"""数据层：SQLite 建库、FTS5 中文检索、Windows DPAPI 凭据加密。

DPAPI 绑定当前 Windows 用户账户：无需主口令，数据库被拷到别的账户/机器
也无法解密。代价是同一账户下的其他进程同样可以调用系统解密。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sqlite3
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("onlyone")


def _base_dir() -> Path:
    """开发期取仓库根目录；PyInstaller 打包后取 EXE 所在目录。

    打包形态下 ``__file__`` 指向 bundle 内部，不能用 parents[2] 推断数据目录，
    否则 data/ 与 frontend/dist/ 会跑到临时解压目录里。
    """
    env = os.environ.get("ONLYONE_BASE_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _base_dir()
DATA_DIR = Path(os.environ.get("ONLYONE_DATA_DIR", str(PROJECT_ROOT / "data")))
DB_PATH = DATA_DIR / "onlyone.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'local',
  enabled INTEGER NOT NULL DEFAULT 1,
  exclude_patterns TEXT NOT NULL DEFAULT '',
  last_scan_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL,
  abs_path TEXT NOT NULL,
  ext TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  mtime REAL NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  extract_status TEXT NOT NULL DEFAULT 'pending',
  extract_error TEXT NOT NULL DEFAULT '',
  indexed_at TEXT,
  UNIQUE(root_id, rel_path)
);
-- 概览页「最近改动」按 mtime 倒序取前 8 条：没有索引时全表排序实测 233ms（113MB 库）。
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  ref_id INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  project_id INTEGER,
  occurred_at TEXT,
  tags TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_entries_type ON entries(type, occurred_at);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_entries USING fts5 (
  entry_id UNINDEXED, title, body, tags, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, url TEXT NOT NULL, project_id INTEGER,
  note TEXT NOT NULL DEFAULT '', snapshot_path TEXT NOT NULL DEFAULT '',
  last_check_at TEXT
);
-- 已停用（2026-11-23）：不再有界面入口与 CRUD API，历史数据原样留在本表，
-- 但对应的 entries / fts 行会在 init_db 的迁移里清掉（退出全局检索）。
CREATE TABLE IF NOT EXISTS worklogs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  log_date TEXT NOT NULL, project_id INTEGER,
  content TEXT NOT NULL DEFAULT '', minutes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
  system TEXT NOT NULL DEFAULT '',
  proposer TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
  symptom TEXT NOT NULL DEFAULT '', root_cause TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
  duration_min INTEGER NOT NULL DEFAULT 0, project_id INTEGER,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS entry_relations (
  src_entry_id INTEGER NOT NULL, dst_entry_id INTEGER NOT NULL,
  relation TEXT NOT NULL DEFAULT 'related',
  PRIMARY KEY (src_entry_id, dst_entry_id, relation)
);
CREATE TABLE IF NOT EXISTS credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  username_enc BLOB, password_enc BLOB, url_enc BLOB, note_enc BLOB,
  project_id INTEGER,
  sap_conn TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- M5-1：正文抽取失败的重试队列；attempts >= 3 即列入前端「失败清单」，需手动重试。
CREATE TABLE IF NOT EXISTS extract_failures (
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- M6-2：语义向量索引（稀疏向量 JSON）；随 entries 级联删除，避免删记录后残留命中。
CREATE TABLE IF NOT EXISTS semantic_index (
  entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
  vector TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- M7：记事本三级结构（笔记本 → 分区 → 页面）。
-- content_json 存 ProseMirror 结构；content_text 是抽取出的纯文本，专供 FTS / 语义索引。
-- is_private=1 的页面不写 fts_entries / semantic_index / entries（完全不参与全局检索）。
CREATE TABLE IF NOT EXISTS notebooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  notebook_id INTEGER NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '未命名页面',
  content_json TEXT NOT NULL DEFAULT '',
  content_text TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  is_private INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 0,
  project_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_notes_section ON notes(section_id);
CREATE INDEX IF NOT EXISTS idx_sections_notebook ON sections(notebook_id);
-- A/B 增量扫描：记录失败队列上次低频重试时间，避免每次启动都重试坏文档。
CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # timeout=30：扫描改成分块提交后（scanner.COMMIT_EVERY_*）写锁只会短暂被占，
    # 但 OCR / WPS 转换单文件可能耗时数秒，留 30s 让并发写请求自动重试而不是直接 500。
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS 不会给已有表补列；凭据绑定 SAP 系统需平滑迁移旧库。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(credentials)")}
        if "sap_conn" not in cols:
            conn.execute("ALTER TABLE credentials "
                         "ADD COLUMN sap_conn TEXT NOT NULL DEFAULT ''")
        # 记事本归属项目（项目工作台需要按项目聚合记事本页）；老库补列 + 补索引。
        note_cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
        if note_cols and "project_id" not in note_cols:
            conn.execute("ALTER TABLE notes ADD COLUMN project_id INTEGER")
            log.info("notes 表已补 project_id 列")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project_id)")
        # 运维记录增加提出人 / 公司 / 类型；老库补列，历史行取空串
        # （类型空值在界面上显示「—」，不强行回填，避免篡改历史记录语义）。
        incident_cols = {r["name"] for r in conn.execute("PRAGMA table_info(incidents)")}
        for column in ("proposer", "company", "category"):
            if incident_cols and column not in incident_cols:
                conn.execute("ALTER TABLE incidents "
                             "ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % column)
                log.info("incidents 表已补 %s 列", column)
        _purge_worklog_index(conn)
        conn.commit()
        log.info("db ready: %s", DB_PATH)
    finally:
        conn.close()


# 工作日志停用标记：清完索引行后写进 app_meta，避免每次启动都全表扫一遍。
_WORKLOG_PURGED_KEY = "worklogs_index_purged_v1"


def _purge_worklog_index(conn) -> None:
    """工作日志退出检索（2026-11-23）：删掉 worklog 条目与 FTS 行，保留 worklogs 表数据。

    ``fts_entries`` 没有外键，必须显式删；``semantic_index`` 随 entries 级联。
    ``entry_relations`` 同样无外键，悬空关联一并清理，避免关系抽屉指向不存在的条目。
    只在首次迁移执行（app_meta 标记），老库重复启动不会反复扫表。
    """
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (_WORKLOG_PURGED_KEY,)).fetchone():
        return
    ids = [r[0] for r in conn.execute("SELECT id FROM entries WHERE type='worklog'")]
    for entry_id in ids:
        conn.execute("DELETE FROM fts_entries WHERE entry_id=?", (entry_id,))
        conn.execute("DELETE FROM entry_relations "
                     "WHERE src_entry_id=? OR dst_entry_id=?", (entry_id, entry_id))
    conn.execute("DELETE FROM entries WHERE type='worklog'")
    conn.execute("INSERT OR REPLACE INTO app_meta(key,value) "
                 "VALUES(?, datetime('now','localtime'))", (_WORKLOG_PURGED_KEY,))
    if ids:
        log.info("工作日志已退出检索：清理 %d 个条目", len(ids))


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_BLOB_PTR = ctypes.POINTER(_Blob)
_BLOB_PTR_DATA = ctypes.POINTER(ctypes.c_byte)
_TAIL_ARGS = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]


def _protect_api():
    """CryptProtectData：第二参数是 LPCWSTR 描述串（值，不是指针）。"""
    fn = ctypes.windll.crypt32.CryptProtectData
    fn.argtypes = [_BLOB_PTR, wintypes.LPCWSTR] + _TAIL_ARGS + [_BLOB_PTR]
    fn.restype = wintypes.BOOL
    return fn


def _unprotect_api():
    """CryptUnprotectData：第二参数是 LPWSTR*（描述串出参），传 None 即可。"""
    fn = ctypes.windll.crypt32.CryptUnprotectData
    fn.argtypes = [_BLOB_PTR, ctypes.POINTER(wintypes.LPCWSTR)] + _TAIL_ARGS + [_BLOB_PTR]
    fn.restype = wintypes.BOOL
    return fn


def _free(pointer) -> None:
    try:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))
    except Exception:  # noqa: BLE001
        pass


def encrypt(text: str) -> bytes:
    """DPAPI 加密（当前 Windows 用户）；空串返回空 BLOB。"""
    raw = (text or "").encode("utf-8")
    if not raw:
        return b""
    buf = ctypes.create_string_buffer(raw)
    src = _Blob(len(raw), ctypes.cast(buf, _BLOB_PTR_DATA))
    out = _Blob()
    fn = _protect_api()
    if not fn(ctypes.byref(src), "OnlyOne", None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _free(out.pbData)


def decrypt(blob: bytes) -> str:
    """DPAPI 解密；失败抛异常，绝不返回猜测值。"""
    if not blob:
        return ""
    raw = bytes(blob)
    buf = ctypes.create_string_buffer(raw)
    src = _Blob(len(raw), ctypes.cast(buf, _BLOB_PTR_DATA))
    out = _Blob()
    fn = _unprotect_api()
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed (数据可能来自其他 Windows 账户或机器)")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        _free(out.pbData)
