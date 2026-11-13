# -*- coding: utf-8 -*-
"""FastAPI 应用工厂。API 只绑 127.0.0.1，令牌中间件保护 /api/*。"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, backends, ocr, watcher
from .backup import router as backup_router
from .business import router as biz_router
from .credentials import router as cred_router
from .notes import router as notes_router
from .db import PROJECT_ROOT, connect, init_db
from .scanner import (
    MAX_ATTEMPTS, progress_snapshot, root_write_lock, scan_root, start_scan_job,
)
from .search import plugin_candidates, search_entries, semantic_candidates

log = logging.getLogger("onlyone")
TOKEN_ENV = "ONLYONE_TOKEN"
STATUS_PATH = "/api/meta/status"


class RootIn(BaseModel):
    path: str
    kind: str = "local"


class OpenIn(BaseModel):
    path: str
    reveal: bool = False


def _conn():
    return connect()


def _refresh_watcher() -> None:
    """根目录变更后重新调度监听；watcher 未启用时为空操作。"""
    try:
        watcher.refresh()
    except Exception as exc:  # noqa: BLE001
        log.warning("watcher refresh 失败: %s", exc)


def _in_roots(conn, target: str) -> bool:
    p = Path(target).resolve()
    for row in conn.execute("SELECT path FROM roots"):
        try:
            p.relative_to(Path(row["path"]).resolve())
            return True
        except ValueError:
            continue
    return False


def _query_terms(q: str) -> list[str]:
    """与前端 highlight() 一致：按空白拆词，全部转小写。"""
    return [t for t in q.lower().split() if t]


def _snippet(text: str, q: str, width: int = 40) -> str:
    """命中片段：只取命中词所在的句子，句子过长时以命中点为中心截断。

    原实现固定取命中词前后各 70 字，容易把无关段落一起带出来；
    改为按句边界收敛，让「看什么」由命中句决定。
    """
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    terms = _query_terms(q)
    low = flat.lower()
    # 多词查询时选「出现次数最少 / 更长」的词作为锚点，避免都落在高频词上
    candidates = [(low.count(t), -len(t), t) for t in terms if t in low]
    target = min(candidates)[2] if candidates else (terms[0] if terms else "")
    i = low.find(target) if target else -1
    if i < 0:
        return flat[: width * 2] + ("…" if len(flat) > width * 2 else "")

    seps = "。！？!?；;\n"
    left = 0
    for sep in seps:
        p = flat.rfind(sep, 0, i)
        if p >= 0:
            left = max(left, p + 1)
    right = len(flat)
    for sep in seps:
        p = flat.find(sep, i + len(target))
        if p >= 0:
            right = min(right, p + 1)
    sentence = flat[left:right].strip()
    if len(sentence) > width * 2:
        j = sentence.lower().find(target)
        a = max(0, j - width // 2)
        b = min(len(sentence), j + len(target) + width)
        sentence = ("…" if a else "") + sentence[a:b] + ("…" if b < len(sentence) else "")
    return ("…" if left else "") + sentence + ("…" if right < len(flat) else "")


def _hit_count(text: str, q: str) -> int:
    """正文里各查询词的出现次数之和，用于「共命中 N 处」。"""
    low = (text or "").lower()
    return sum(low.count(t) for t in _query_terms(q))


def _items_by_ids(conn, ids: list[int], q: str) -> list[dict]:
    """把 entry_id 列表组装成搜索结果项（字面 / 语义分组共用）。"""
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    bodies = {r["entry_id"]: r["body"] for r in conn.execute(
        f"SELECT entry_id, body FROM fts_entries WHERE entry_id IN ({marks})", ids)}
    rows = conn.execute(
        f"""SELECT e.*, f.abs_path, f.ext, f.extract_status, f.extract_error,
                   r.kind AS root_kind
            FROM entries e
            LEFT JOIN files f ON f.id = e.ref_id AND e.type='file'
            LEFT JOIN roots r ON r.id = f.root_id
            WHERE e.id IN ({marks}) ORDER BY e.updated_at DESC""", ids)
    items = []
    terms = _query_terms(q)
    for row in rows:
        item = dict(row)
        body = bodies.get(row["id"], "") or ""
        item["snippet"] = _snippet(body or item.get("summary", "") or "", q)
        item["hit_count"] = _hit_count(body, q)
        title_low = str(row["title"] or "").lower()
        item["hit_in_title"] = any(t in title_low for t in terms)
        items.append(item)
    return items


def create_app(token: str) -> FastAPI:
    init_db()
    app = FastAPI(title="OnlyOne", version=__version__, docs_url="/api/docs")
    app.include_router(biz_router)    # M3：业务记录 CRUD / 关联 / 链接打开
    app.include_router(cred_router)   # M4：DPAPI 凭据库
    app.include_router(notes_router)  # M7：记事本 / 图片 / 导出 / 全库备份
    app.include_router(backup_router) # M7：凭据加密导出与导入（C 方案）

    @app.middleware("http")
    async def token_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/attachments/"):
            # 图片由 <img src> 直接加载，无法带自定义头；本地随机端口 + 一次性令牌
            # 已保护其余 API。单独放行可避免令牌被写进 content_json 与导出文件。
            return await call_next(request)
        if path.startswith("/api/") and path != STATUS_PATH and not path.startswith("/api/docs"):
            # 其余 API 也可通过 ?token= 传令牌（与静态页 /?token= 一致）
            # （静态页面本来就通过 /?token= 打开，令牌不进入日志）。
            supplied = (request.headers.get("X-OnlyOne-Token")
                        or request.query_params.get("token"))
            if supplied != token:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get(STATUS_PATH)
    def status():
        conn = _conn()
        try:
            counts = {
                "roots": conn.execute("SELECT count(*) FROM roots").fetchone()[0],
                "files": conn.execute("SELECT count(*) FROM files").fetchone()[0],
                "entries": conn.execute("SELECT count(*) FROM entries").fetchone()[0],
            }
        finally:
            conn.close()
        return {"ok": True, "app": "OnlyOne", "version": __version__, "counts": counts}

    @app.get("/api/roots")
    def list_roots():
        conn = _conn()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM roots ORDER BY id")]
        finally:
            conn.close()

    @app.post("/api/roots")
    def add_root(body: RootIn):
        p = Path(body.path.strip())
        if not p.is_dir():
            raise HTTPException(400, "目录不存在：%s" % p)
        if body.kind not in ("local", "anyshare_sync"):
            raise HTTPException(400, "kind 只能是 local 或 anyshare_sync")
        conn = _conn()
        try:
            cur = conn.execute("INSERT INTO roots(path,kind) VALUES(?,?)", (str(p.resolve()), body.kind))
            conn.commit()
            _refresh_watcher()
            return {"id": cur.lastrowid, "path": str(p.resolve()), "kind": body.kind}
        except Exception as exc:
            raise HTTPException(400, "该目录已登记或写入失败：%s" % exc) from exc
        finally:
            conn.close()

    @app.delete("/api/roots/{root_id}")
    def del_root(root_id: int):
        """移除索引根目录及其全部索引数据（不删除磁盘文件）。

        集合式删除：原先逐文件循环时，每条 FTS5 DELETE 都要全表扫描一次
        （实测 89.7ms/文件，4928 个文件需约 442 秒且全程独占写锁），
        与后台扫描并发会触发 database is locked → 裸 500。
        semantic_index / extract_failures 由外键 ON DELETE CASCADE 自动清理。
        """
        conn = _conn()
        try:
            if conn.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone() is None:
                raise HTTPException(404, "索引根目录不存在：%s" % root_id)
            sel = ("SELECT e.id FROM entries e JOIN files f "
                   "ON f.id = e.ref_id AND e.type='file' WHERE f.root_id=?")
            try:
                with root_write_lock(conn, root_id):
                    conn.execute(
                        "DELETE FROM fts_entries WHERE entry_id IN (" + sel + ")",
                        (root_id,))
                    # entry_relations 没有外键约束，需显式清理悬空关联
                    conn.execute(
                        "DELETE FROM entry_relations WHERE src_entry_id IN (" + sel
                        + ") OR dst_entry_id IN (" + sel + ")",
                        (root_id, root_id),
                    )
                    conn.execute(
                        "DELETE FROM entries WHERE type='file' AND ref_id IN "
                        "(SELECT id FROM files WHERE root_id=?)", (root_id,))
                    conn.execute("DELETE FROM files WHERE root_id=?", (root_id,))
                    conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
                    conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise HTTPException(400, "移除失败：%s" % exc) from exc
            _refresh_watcher()
            return {"deleted": root_id}
        finally:
            conn.close()

    @app.post("/api/roots/{root_id}/scan")
    def do_scan(root_id: int, wait: bool = True):
        """默认同步返回统计（回归脚本沿用旧契约）；前端传 ``?wait=false`` 走后台任务。

        后台模式下立即返回，前端轮询 ``/api/scan/status`` 显示进度条——
        全量扫描可能持续数分钟，同步请求会让前端一直挂起直到网络层超时。
        """
        if not wait:
            ok, msg = start_scan_job(root_id)
            if not ok:
                raise HTTPException(409, msg)
            return {"started": True, "root_id": root_id}
        conn = _conn()
        try:
            return scan_root(conn, root_id)
        except (KeyError, NotADirectoryError) as exc:
            raise HTTPException(404, str(exc)) from exc
        finally:
            conn.close()

    @app.get("/api/scan/status")
    def scan_status():
        """M8：扫描进度（百分比 / 当前文件 / 预计剩余秒数）。"""
        return progress_snapshot()

    @app.post("/api/roots/{root_id}/rebuild")
    def rebuild_root(root_id: int, wait: bool = True):
        """M5-2：忽略 size+mtime 判定，强制重建该目录的正文索引。"""
        if not wait:
            ok, msg = start_scan_job(root_id, force=True)
            if not ok:
                raise HTTPException(409, msg)
            return {"started": True, "root_id": root_id}
        conn = _conn()
        try:
            return scan_root(conn, root_id, force=True)
        except (KeyError, NotADirectoryError) as exc:
            raise HTTPException(404, str(exc)) from exc
        finally:
            conn.close()

    @app.get("/api/failures")
    def list_failures(given_up: bool = False):
        conn = _conn()
        try:
            sql = """SELECT ef.file_id, ef.attempts, ef.last_error, ef.updated_at,
                            f.rel_path, f.abs_path, f.root_id, f.extract_status,
                            r.path AS root_path
                     FROM extract_failures ef
                     JOIN files f ON f.id = ef.file_id
                     JOIN roots r ON r.id = f.root_id"""
            if given_up:
                sql += " WHERE ef.attempts >= ?"
            sql += " ORDER BY ef.updated_at DESC, ef.file_id DESC"
            params = (MAX_ATTEMPTS,) if given_up else ()
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    @app.post("/api/failures/{file_id}/retry")
    def retry_failure(file_id: int, wait: bool = True):
        conn = _conn()
        try:
            row = conn.execute("SELECT root_id FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "文件记录不存在：%s" % file_id)
            conn.execute(
                "UPDATE extract_failures SET attempts=0, "
                "updated_at=datetime('now','localtime') WHERE file_id=?", (file_id,))
            conn.commit()
            root_id = row["root_id"]
        finally:
            conn.close()
        if not wait:
            ok, msg = start_scan_job(root_id)
            if not ok:
                raise HTTPException(409, msg)
            return {"started": True, "root_id": root_id}
        conn = _conn()
        try:
            return scan_root(conn, root_id)
        finally:
            conn.close()

    @app.get("/api/watcher/status")
    def watcher_status():
        return watcher.status()

    @app.get("/api/ocr/status")
    def ocr_status():
        """M6-1：当前 OCR 引擎与能力边界。"""
        return ocr.status()

    @app.get("/api/backends/status")
    def backends_status():
        """M6-3：可选外部索引后端，默认关闭。"""
        return backends.status()

    @app.get("/api/search")
    def search(q: str = ""):
        """字面检索 + 语义候选 + 外部索引，三组结果独立返回、分数不混排。"""
        q = q.strip()
        if not q:
            return {"query": "", "mode": "none", "items": [],
                    "semantic_items": [], "plugin_items": []}
        conn = _conn()
        try:
            mode, ids = search_entries(conn, q)
            items = _items_by_ids(conn, ids, q)
            # C 方案：标题命中的结果置顶，其次按正文命中次数降序
            items.sort(key=lambda it: (0 if it.get("hit_in_title") else 1,
                                      -int(it.get("hit_count") or 0)))

            # M6-2：排除字面已命中的条目，避免重复；按向量分数排序
            candidates = semantic_candidates(conn, q, exclude_ids=set(ids))
            score_map = dict(candidates)
            semantic_items = _items_by_ids(conn, [eid for eid, _ in candidates], q)
            for item in semantic_items:
                item["score"] = score_map.get(item["id"], 0.0)
                item["via"] = "semantic"
            semantic_items.sort(key=lambda item: -item["score"])
        finally:
            conn.close()

        # M6-3：外部索引只读、默认关闭、独立分组
        plugin_items = plugin_candidates(q)
        return {"query": q, "mode": mode, "items": items,
                "semantic_items": semantic_items, "plugin_items": plugin_items}

    @app.post("/api/open")
    def open_path(body: OpenIn):
        """只读方案的核心：把文件交回本机 Office/WPS，或在资源管理器中定位。"""
        p = Path(body.path)
        if not p.exists():
            raise HTTPException(404, "路径不存在：%s" % p)
        conn = _conn()
        try:
            allowed = _in_roots(conn, str(p))
        finally:
            conn.close()
        if not allowed:
            raise HTTPException(403, "只能打开已登记索引根目录下的文件")
        try:
            if body.reveal:
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(p.resolve())])
            else:
                os.startfile(str(p.resolve()))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, "打开失败：%s" % exc) from exc
        return {"opened": str(p.resolve()), "reveal": body.reveal}

    @app.get("/api/preview")
    def preview(entry_id: int):
        """只读预览：返回抽取的正文与表格，不写回文件、不缓存副本。"""
        conn = _conn()
        try:
            row = conn.execute(
                """SELECT e.*, f.abs_path, f.ext, f.extract_status, f.extract_error,
                          r.kind AS root_kind
                   FROM entries e
                   LEFT JOIN files f ON f.id = e.ref_id AND e.type='file'
                   LEFT JOIN roots r ON r.id = f.root_id
                   WHERE e.id=?""", (entry_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "条目不存在：%s" % entry_id)
            data = dict(row)
            allowed = _in_roots(conn, data["abs_path"]) if data["abs_path"] else False
            fts = conn.execute("SELECT body FROM fts_entries WHERE entry_id=?",
                               (data["id"],)).fetchone()
            body_text = fts["body"] if fts else ""
        finally:
            conn.close()

        if data["type"] != "file":
            return {"title": data["title"], "text": body_text or data["summary"], "tables": [],
                    "error": "", "source": data.get("tags") or "业务记录", "abs_path": None,
                    "extract_status": "", "root_kind": None}
        if not data["abs_path"] or not allowed:
            raise HTTPException(403, "只能预览已登记索引根目录下的文件")

        from .extractor import extract

        result = extract(Path(data["abs_path"]))
        return {
            "title": data["title"],
            "text": result.text,
            "tables": result.tables,
            "error": result.error or data["extract_error"] or "",
            "source": "爱数同步盘" if data["root_kind"] == "anyshare_sync" else "本地",
            "abs_path": data["abs_path"],
            "extract_status": data["extract_status"],
            "root_kind": data["root_kind"],
        }

    dist = PROJECT_ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    else:
        log.warning("前端未构建：%s", dist)

        @app.get("/", response_class=HTMLResponse)
        def no_ui():
            return ("<h3>OnlyOne 后端已启动</h3>"
                    "<p>前端尚未构建。请在 frontend 目录执行 <code>npm run build</code> 后重启，"
                    "或在开发期用 <code>npm run dev</code> 打开 5179 端口。</p>")

    return app
