# -*- coding: utf-8 -*-
"""OneNote 导出的单文件网页（.mht）→ OnlyOne 记事本（命令行版）。

解析与转换复用后端模块 ``onlyone.mht_import``（桌面端「导入」按钮走的是同一份
代码），本文件只负责：扫描输入、调后端 HTTP 建本 / 分区 / 页面、上传图片。

支持两种导出粒度：
  单节：``--input D:\\1118\\MM.MHT``           → 一个分区
  整本：``--input D:\\1118\\SAP\\``（目录）     → 目录下每个 .mht/.mhtml 一个分区

用法：
  py -3 tools\\import_mht_notes.py --input D:\\1118\\MM.MHT                 # 只分析
  py -3 tools\\import_mht_notes.py --input D:\\1118\\SAP\\ --write --book "SAP 笔记"
  py -3 tools\\import_mht_notes.py --input "D:\\1118\\*.MHT" --write
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from onlyone.mht_import import IMG_SCHEME, build_pages  # noqa: E402

MAX_IMAGE = 10 * 1024 * 1024


class Api:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, path: str, method: str = "GET", body: Any = None,
             timeout: int = 180, retries: int = 3) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(
                self.base + path, data=data, method=method,
                headers={"X-OnlyOne-Token": self.token,
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code < 500:      # 4xx 是业务错误，重试没意义
                    raise
                last = exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
            time.sleep(0.6 * (attempt + 1))
        raise last if last else RuntimeError("请求失败：%s" % path)


def collect_inputs(raw_inputs: list[str]) -> list[Path]:
    """文件 / 目录 / 通配符 → 去重后的 mht 列表。

    目录只扫 ``*.mht`` / ``*.mhtml``：整本导出目录里可能还有 index.htm 之类
    的导航页，把 .htm 也算进去会凭空多出一个分区。单个 .htm 仍可显式指定。
    """
    found: list[Path] = []
    for item in raw_inputs:
        path = Path(item)
        if path.is_dir():
            for ext in ("*.mht", "*.mhtml"):
                found.extend(sorted(path.rglob(ext)))
        elif path.is_file():
            found.append(path)
        else:
            for match in sorted(glob.glob(item)):
                mp = Path(match)
                if mp.is_file():
                    found.append(mp)
    seen: set[str] = set()
    uniq: list[Path] = []
    for path in found:
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


def upload_images(api: Api, note_id: int, doc: dict,
                  images: dict[str, tuple[bytes, str]]
                  ) -> tuple[dict, list[str], int]:
    """把 __mht__:xxx 占位换成真实 URL；缺失 / 超限的图片节点直接摘掉。"""
    missing: list[str] = []
    uploaded = 0
    url_cache: dict[str, str] = {}

    def fix_image(node: dict) -> bool:
        nonlocal uploaded
        src = str((node.get("attrs") or {}).get("src") or "")
        if not src.startswith(IMG_SCHEME):
            return True
        name = src[len(IMG_SCHEME):]
        if name in url_cache:
            node["attrs"]["src"] = url_cache[name]
            return True
        item = images.get(name)
        if item is None:
            missing.append(name)
            return False
        payload, mime = item
        if len(payload) > MAX_IMAGE:
            missing.append(name + "（超过 10 MB）")
            return False
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(payload).decode())
        try:
            result = api.call("/api/notes/%d/images" % note_id, "POST",
                              {"data": data_url, "name": name})
        except Exception:                       # noqa: BLE001
            missing.append(name + "（上传失败）")
            return False
        url_cache[name] = result["url"]
        node["attrs"]["src"] = result["url"]
        uploaded += 1
        return True

    def walk(node) -> None:
        children = node.get("content")
        if not isinstance(children, list):
            return
        kept = []
        for child in children:
            if isinstance(child, dict):
                if child.get("type") == "image" and not fix_image(child):
                    continue
                walk(child)
                if child.get("type") == "paragraph" and not child.get("content"):
                    continue        # 图片摘掉后段落空了就一并去掉
            kept.append(child)
        node["content"] = kept

    walk(doc)
    return doc, missing, uploaded


def ensure_section(api: Api, book_name: str, section_name: str) -> int:
    books = api.call("/api/notebooks")
    book = next((b for b in books if b["name"] == book_name), None)
    if book is None:
        book = api.call("/api/notebooks", "POST", {"name": book_name})
    sections = book.get("sections") or []
    name = section_name
    if any(s["name"] == name for s in sections):     # 重复导入 → 加序号
        names = {s["name"] for s in sections}
        index = 2
        while ("%s (%d)" % (section_name, index)) in names:
            index += 1
        name = "%s (%d)" % (section_name, index)
    created = api.call("/api/sections", "POST",
                       {"notebook_id": book["id"], "name": name})
    return created["id"]


def process_file(path: Path, api: Api | None, book: str,
                 section_override: str) -> dict:
    raw = path.read_bytes()
    pages, images = build_pages(raw)
    section = section_override or path.stem
    report: dict[str, Any] = {"file": str(path), "section": section,
                              "pages": 0, "images": len(images),
                              "uploaded": 0, "missing": [], "note_ids": []}

    print("%s → 分区「%s」：%d 页，内嵌图 %d 张"
          % (path.name, section, len(pages), len(images)))
    for index, (title, page_doc) in enumerate(pages, 1):
        tables = [b for b in page_doc["content"] if b["type"] == "table"]
        rows = sum(len(b["content"]) for b in tables)
        img_count = json.dumps(page_doc, ensure_ascii=False).count(IMG_SCHEME)
        print("    P%-2d %-32s 块=%3d 表=%d 行=%3d 图=%d"
              % (index, title[:32], len(page_doc["content"]), len(tables), rows, img_count))
    report["pages"] = len(pages)

    if api is None:
        return report

    section_id = ensure_section(api, book, section)
    for title, page_doc in pages:
        note = api.call("/api/notes", "POST",
                        {"section_id": section_id, "title": title})
        note_id = note["id"]
        fixed, missing, uploaded = upload_images(api, note_id, page_doc, images)
        report["uploaded"] += uploaded
        report["missing"].extend(missing)
        if missing:
            print("       ! 缺图 %d 张：%s" % (len(missing), missing[:3]))
        api.call("/api/notes/%d" % note_id, "PUT", {
            "title": title,
            "content_json": json.dumps(fixed, ensure_ascii=False),
            "is_private": False,
            "revision": 0,
        })
        report["note_ids"].append(note_id)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "--mht", dest="inputs", nargs="+", required=True,
                    help="单个 .mht / 目录（整本） / 多个文件 / 通配符")
    ap.add_argument("--write", action="store_true", help="写入后端（默认只分析）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--token", default=os.environ.get("ONLYONE_TOKEN", ""))
    ap.add_argument("--book", default="SAP 笔记")
    ap.add_argument("--section", default="", help="仅单文件时覆盖分区名")
    args = ap.parse_args()

    files = collect_inputs(args.inputs)
    if not files:
        print("没有找到 .mht / .htm 文件")
        return 1
    if len(files) > 1 and args.section:
        print("多文件输入时不支持 --section，分区名取各自文件名")
        args.section = ""

    api = None
    if args.write:
        if not args.token:
            print("缺少 token：--token 或环境变量 ONLYONE_TOKEN")
            return 1
        api = Api(args.base, args.token)

    print("共 %d 个文件，目标笔记本「%s」%s\n"
          % (len(files), args.book, "（写库）" if api else "（仅分析）"))
    reports: list[dict] = []
    for path in files:
        try:
            reports.append(process_file(path, api, args.book, args.section))
        except Exception as exc:                # noqa: BLE001
            print("  FAIL %s: %s" % (path.name, exc))
            reports.append({"file": str(path), "error": str(exc)})
            continue
        print()

    total_pages = sum(r.get("pages", 0) for r in reports)
    total_imgs = sum(r.get("images", 0) for r in reports)
    uploaded = sum(r.get("uploaded", 0) for r in reports)
    missing = [m for r in reports for m in r.get("missing", [])]
    failed = [r for r in reports if r.get("error")]
    print("汇总：%d 个文件 / %d 页 / 内嵌图 %d 张%s"
          % (len(reports), total_pages, total_imgs,
             ("，已上传 %d 张" % uploaded) if api else ""))
    if failed:
        print("失败 %d 个文件：%s" % (len(failed),
                                   [(Path(r["file"]).name, r["error"][:60]) for r in failed]))
    if missing:
        print("缺图 %d 张：%s" % (len(missing), missing[:6]))
    if api is None:
        print("\n（未写库。确认无误后加 --write --book <笔记本>）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
