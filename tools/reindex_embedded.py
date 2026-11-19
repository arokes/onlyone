# -*- coding: utf-8 -*-
"""定向重建：只重抽「docx 内含 word/embeddings/ 对象」的文件。

为什么需要它：v0.9 起 ``extractor`` 会读取 docx 内嵌 Excel（OLE ``package`` 流、
``Workbook`` 流、以及直接嵌入的 ``.xlsx`` 部件），但增量扫描按 size+mtime 判定，
老文件不会自动重抽；全量 rebuild 会把 300+ PDF 的 OCR 一并重跑，代价过大。

用法：
    py -3 tools/reindex_embedded.py            # 实际重抽
    py -3 tools/reindex_embedded.py --dry-run  # 只列出目标文件
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from onlyone.db import connect  # noqa: E402
from onlyone.extractor import extract  # noqa: E402
from onlyone.scanner import SENSITIVE_PAT, _drop_entry, _sync_entry  # noqa: E402


def has_embedding(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return any(n.startswith("word/embeddings/") and not n.endswith("/")
                       for n in z.namelist())
    except Exception:  # noqa: BLE001 - 损坏 / 非 zip 一律跳过
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="重抽含内嵌对象的 docx")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不写库")
    args = ap.parse_args()

    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, root_id, rel_path, abs_path FROM files WHERE ext='.docx'")]
        targets = [r for r in rows if Path(r["abs_path"]).is_file() and has_embedding(r["abs_path"])]
        print("docx 总数 %d，含内嵌对象 %d" % (len(rows), len(targets)))
        for row in targets:
            print("  -", row["rel_path"])
        if args.dry_run or not targets:
            return 0

        for idx, row in enumerate(targets, 1):
            full = Path(row["abs_path"])
            fid = int(row["id"])
            name = full.name
            if SENSITIVE_PAT.search(name) or SENSITIVE_PAT.search(row["rel_path"]):
                print("[%d/%d] 跳过敏感文件 %s" % (idx, len(targets), name))
                _drop_entry(conn, fid)
                conn.execute("UPDATE files SET extract_status='skipped_sensitive' WHERE id=?",
                             (fid,))
                conn.commit()
                continue
            result = extract(full)
            body = result.body or name
            status = "failed" if result.error else "ok"
            conn.execute(
                "UPDATE files SET extract_status=?, extract_error=?, "
                "indexed_at=datetime('now','localtime') WHERE id=?",
                (status, result.error[:500], fid))
            _sync_entry(conn, fid, name, body)
            conn.commit()
            print("[%d/%d] %-6s 正文 %7d 字 %s %s" % (
                idx, len(targets), status, len(body), name,
                ("· " + result.error[:60]) if result.error else ""))

        hit = conn.execute(
            "SELECT COUNT(*) FROM fts_entries WHERE body LIKE ?",
            ("%物料类型规则%",)).fetchone()[0]
        print("校验：搜「物料类型规则」命中 %d 条" % hit)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
