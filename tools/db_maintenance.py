# -*- coding: utf-8 -*-
"""索引库维护工具：FTS5 段合并 + 空间回收。

用途：`data/onlyone.db` 因反复重建索引而膨胀时，把死段与空闲页回收掉。
实测（1367 条条目）：309.1 MB → 108.1 MB，内容与检索结果不变。

**运行前请退出 OnlyOne**：VACUUM 需要独占锁，应用运行时多半拿不到；
拿不到时本工具会明确报「库被占用，请先退出 OnlyOne」，不会破坏数据。

用法：
    py -3 tools/db_maintenance.py             # optimize + VACUUM（推荐）
    py -3 tools/db_maintenance.py --dry-run   # 只看指标，不写库
    py -3 tools/db_maintenance.py --no-vacuum # 只合并 FTS 段（不动磁盘空间）
    py -3 tools/db_maintenance.py --db D:\\other\\onlyone.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def fmt(mb: float) -> str:
    return "%.1f MB" % mb


def show(title: str, st: dict) -> None:
    print(title)
    print("  文件大小      %8s" % fmt(st["file_mb"]))
    print("  有效数据      %8s" % fmt(st["used_mb"]))
    print("  空闲页        %8s（%d 页，占 %.0f%%）"
          % (fmt(st["free_mb"]), st["free_pages"], st["free_ratio"] * 100))
    print("  条目数        %8d（语义向量 %d 条）" % (st["entries"], st["semantic_rows"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="OnlyOne 索引库维护")
    ap.add_argument("--dry-run", action="store_true", help="只输出指标，不写库")
    ap.add_argument("--no-vacuum", action="store_true", help="只 optimize，不 VACUUM")
    ap.add_argument("--db", default="", help="指定数据库路径（默认 data/onlyone.db）")
    args = ap.parse_args()

    if args.db:
        import os
        p = Path(args.db).resolve()
        if not p.is_file():
            print("找不到数据库：%s" % p)
            return 2
        # db.py 在 import 时按 ONLYONE_DATA_DIR 决定路径，这里必须在 import 前设置
        os.environ["ONLYONE_DATA_DIR"] = str(p.parent)

    from onlyone.db import DB_PATH
    from onlyone.maintenance import compact, stats

    print("数据库：%s" % DB_PATH)
    before = stats()
    show("维护前", before)

    if args.dry_run:
        print("\n--dry-run：未写库。预计可回收约 %s（运行本工具不带参数即执行）。"
              % fmt(before["free_mb"]))
        return 0

    print("\n正在 optimize%s ……" % (" + VACUUM" if not args.no_vacuum else ""))
    res = compact(vacuum=not args.no_vacuum, timeout=20)

    if not res.get("ok"):
        print("\n未完成：%s" % res.get("reason", "未知原因"))
        if "locked" in (res.get("reason") or "").lower() or "占用" in (res.get("reason") or ""):
            print("→ 请先退出 OnlyOne，再重跑本命令。")
        return 1

    after = res["after"]
    print()
    show("维护后", after)
    saved = res["before"]["file_mb"] - after["file_mb"]
    print("\n完成：%s → %s，回收 %s（-%.0f%%），耗时 %.1fs"
          % (fmt(res["before"]["file_mb"]), fmt(after["file_mb"]), fmt(saved),
             saved / res["before"]["file_mb"] * 100 if res["before"]["file_mb"] else 0,
             res.get("cost_sec", 0)))
    if res.get("reason"):
        print("附注：%s" % res["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
