# -*- coding: utf-8 -*-
"""OneNote 导出的单文件网页（.mht）→ ProseMirror/TipTap 页面。

设计要点：

* 纯解析，不碰数据库与磁盘——落库、图片写盘由 ``notes.py`` 的路由负责，
  命令行工具 ``tools/import_mht_notes.py`` 也复用本模块，避免两份实现漂移。
* OneNote「单个文件网页」的产物：一节一个 .mht（整本导出则是一个目录）；
  节内每页是一个 ``div[style*=border-width:100%]``，页面标题是该 div 里
  第一个 ≥16pt 的 p，紧随其后 ``#767676`` + 10pt 的灰字是创建时间（丢弃）。
* 图片是 multipart/related 里的独立分片（``imageNNN.png``），HTML 用相对
  路径引用，所以位置可以准确还原。转换期用 ``__mht__:文件名`` 占位，
  由调用方替换成真实 URL。
* 整本导出为**单个** .mht 时，节边界在文件里完全不存在（页间恒定 3 个空
  段落，页面 div 无 id/name），因此只能整本进一个分区。
"""
from __future__ import annotations

import email
import re
from email import policy
from typing import Any

from lxml import html as LH

PAGE_MARK = "border-width:100%"
FONT_PT = re.compile(r"font-size:\s*([\d.]+)pt", re.I)
TITLE_MIN_PT = 16.0          # 页面标题字号下限（OneNote 默认 20pt）
META_MAX_PT = 11.0           # 创建时间等元数据上限
META_COLOR = "#767676"
DATE_LIKE = re.compile(r"^\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
TIME_LIKE = re.compile(r"^\s*\d{1,2}:\d{2}\s*$")
IMG_SCHEME = "__mht__:"      # 转换期图片占位；落库时替换成真实 URL
MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "image/webp": "webp", "image/bmp": "bmp"}


# ---------------------------------------------------------------- MHT 解析

def parse_mht(data: bytes) -> tuple[str, dict[str, tuple[bytes, str]]]:
    """返回 (html 文本, {图片文件名: (字节, MIME)})。"""
    msg = email.message_from_bytes(data, policy=policy.default)
    html_part = None
    images: dict[str, tuple[bytes, str]] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype == "text/html" and html_part is None:
            html_part = part
            continue
        if ctype.startswith("image/"):
            payload = part.get_payload(decode=True) or b""
            if not payload:
                continue
            loc = str(part.get("Content-Location") or "")
            name = loc.replace("\\", "/").rsplit("/", 1)[-1]
            if not name:
                name = "image%03d.%s" % (len(images) + 1, MIME_EXT.get(ctype, "png"))
            images[name] = (payload, ctype)
    if html_part is None:
        raise ValueError("MHT 里没有 text/html 部分")
    charset = html_part.get_content_charset() or "utf-8"
    return html_part.get_payload(decode=True).decode(charset, "replace"), images


def split_pages(html_text: str) -> list:
    doc = LH.fromstring(html_text)
    body = doc.find("body")
    root = body if body is not None else doc
    pages = [el for el in root if el.tag == "div" and PAGE_MARK in (el.get("style") or "")]
    if not pages:                       # 少数导出多包一层 div
        pages = [el for el in root.iter("div")
                 if PAGE_MARK in (el.get("style") or "")
                 and not any(PAGE_MARK in (a.get("style") or "") for a in el.iterancestors())]
    return pages


def style_pt(el) -> float:
    m = FONT_PT.search(el.get("style") or "")
    return float(m.group(1)) if m else 0.0


def clean_text(raw: str) -> str:
    text = (raw or "").replace("\xa0", " ").replace("\u200b", "")
    text = text.replace("\r", "").replace("\n", " ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def is_meta_para(p) -> bool:
    """创建时间 / 页码这类灰色小字，不进正文。"""
    style = p.get("style") or ""
    pt = style_pt(p)
    text = clean_text(p.text_content())
    if not text:
        return False
    if META_COLOR in style.lower() and pt and pt <= META_MAX_PT:
        return True
    return bool(DATE_LIKE.match(text) or TIME_LIKE.match(text))


# ---------------------------------------------------------------- 节点转换

def image_node(img) -> dict | None:
    src = (img.get("src") or "").replace("\\", "/")
    if not src:
        return None
    attrs: dict[str, Any] = {"src": IMG_SCHEME + src.rsplit("/", 1)[-1],
                             "alt": img.get("alt") or "图片"}
    try:                                # 保留原始显示宽度，大图有个合理上限
        width = int(float(img.get("width") or 0))
        if 40 <= width <= 1200:
            attrs["width"] = width
    except (TypeError, ValueError):
        pass
    return {"type": "image", "attrs": attrs}


def inline_content(el) -> list[dict[str, Any]]:
    """段落 → TipTap 行内节点：文本（保留粗体/斜体）+ 图片。"""
    nodes: list[dict[str, Any]] = []

    def push_text(text: str, marks: list[dict]) -> None:
        text = text.replace("\xa0", " ").replace("\u200b", "")
        if not text:
            return
        node: dict[str, Any] = {"type": "text", "text": text}
        if marks:
            node["marks"] = marks
        if nodes and nodes[-1].get("type") == "text" \
                and nodes[-1].get("marks", []) == marks:
            nodes[-1]["text"] += text
        else:
            nodes.append(node)

    def walk(node, marks: list[dict]) -> None:
        tag = node.tag if isinstance(node.tag, str) else ""
        style = (node.get("style") or "").lower()
        inherited = list(marks)
        if tag in ("b", "strong") or re.search(r"font-weight:\s*(bold|[6-9]00)", style):
            if not any(m["type"] == "bold" for m in inherited):
                inherited.append({"type": "bold"})
        if tag in ("i", "em") or "font-style:italic" in style:
            if not any(m["type"] == "italic" for m in inherited):
                inherited.append({"type": "italic"})
        if tag == "img":
            node_img = image_node(node)
            if node_img:
                nodes.append(node_img)
            return
        if tag == "br":
            push_text("\n", inherited)
            return
        if node.text:
            push_text(node.text, inherited)
        for child in node:
            walk(child, inherited)
            if child.tail:
                push_text(child.tail, inherited)

    walk(el, [])
    merged: list[dict[str, Any]] = []
    for node in nodes:
        if node["type"] == "text" and merged and merged[-1].get("type") == "text" \
                and merged[-1].get("marks", []) == node.get("marks", []):
            merged[-1]["text"] += node["text"]
        else:
            merged.append(node)
    return [n for n in merged if n["type"] == "image" or n.get("text", "").strip()]


def para_node(p) -> dict | None:
    if is_meta_para(p):
        return None
    content = inline_content(p)
    if not content:
        return None
    has_image = any(n["type"] == "image" for n in content)
    if not has_image and not "".join(n.get("text", "") for n in content).strip():
        return None
    return {"type": "paragraph", "content": content}


def table_node(table) -> dict | None:
    row_nodes = []
    for index, tr in enumerate(table.xpath("./tr | ./tbody/tr")):
        cell_nodes = []
        for td in tr.xpath("./td | ./th"):
            kind = "tableHeader" if (index == 0 or td.tag == "th") else "tableCell"
            attrs: dict[str, Any] = {"colspan": 1, "rowspan": 1, "colwidth": None}
            for name in ("colspan", "rowspan"):
                try:
                    value = int(td.get(name) or 1)
                    if value > 1:
                        attrs[name] = value
                except ValueError:
                    pass
            inner: list[dict] = []
            for p in td.xpath("./p | ./div/p"):
                node = para_node(p)
                if node:
                    inner.append(node)
            if not inner:                       # 单元格里只有裸文本 / 图片
                content = inline_content(td)
                if content:
                    inner = [{"type": "paragraph", "content": content}]
            if not inner:
                inner = [{"type": "paragraph"}]  # 空单元格要留位，否则列错位
            cell_nodes.append({"type": kind, "attrs": attrs, "content": inner})
        if cell_nodes:
            row_nodes.append({"type": "tableRow", "content": cell_nodes})
    if not row_nodes:
        return None
    return {"type": "table", "content": row_nodes}


def list_block(node) -> dict | None:
    """ul/ol → bulletList/orderedList；li 里只有图片时就地平铺，不加项目符号。"""
    ordered = node.tag == "ol"
    items: list[dict] = []
    only_images = True
    for li in node.xpath("./li"):
        inner: list[dict] = []
        for child in li:
            if not isinstance(child.tag, str):
                continue
            if child.tag == "p":
                block = para_node(child)
                if block:
                    inner.append(block)
            elif child.tag in ("ul", "ol"):
                nested = list_block(child)
                if nested:
                    inner.append(nested)
                    only_images = False
            elif child.tag == "table":
                block = table_node(child)
                if block:
                    inner.append(block)
                    only_images = False
        if not inner:
            continue
        if not any(b["type"] != "paragraph"
                   or any(n.get("type") != "image" for n in b.get("content") or [])
                   for b in inner):
            continue                    # 纯图片的 li 交给外层按段落平铺
        only_images = False
        items.append({"type": "listItem", "content": inner})
    if only_images or not items:
        return None
    return {"type": "orderedList" if ordered else "bulletList", "content": items}


def page_title(page) -> tuple[str, bool]:
    for p in page.xpath(".//p"):
        if style_pt(p) >= TITLE_MIN_PT:
            title = clean_text(p.text_content())
            if title:
                return title, True
    return "", False


def page_to_doc(page) -> tuple[str, dict]:
    title, found = page_title(page)
    blocks: list[dict] = []
    title_id: int | None = None
    if found:
        for p in page.xpath(".//p"):
            if style_pt(p) >= TITLE_MIN_PT and clean_text(p.text_content()):
                title_id = id(p)
                break

    def walk(node) -> None:
        tag = node.tag if isinstance(node.tag, str) else ""
        if tag == "p":
            if title_id is not None and id(node) == title_id:
                return
            if node.xpath("ancestor::table"):
                return
            block = para_node(node)
            if block:
                blocks.append(block)
            return
        if tag == "table":
            block = table_node(node)
            if block:
                blocks.append(block)
            return
        if tag == "img":                    # 图片直接挂在容器上（不在 p 里）
            node_img = image_node(node)
            if node_img:
                blocks.append({"type": "paragraph", "content": [node_img]})
            return
        if tag in ("ul", "ol"):
            block = list_block(node)
            if block:
                blocks.append(block)
                return
            for child in node:              # 纯图片列表 → 平铺
                walk(child)
            return
        if tag == "li":                     # 兜底：脱离 ul 的 li
            for child in node:
                walk(child)
            return
        if not isinstance(node.tag, str):
            return
        for child in node:                  # 其余容器一律递归，不设白名单
            walk(child)

    walk(page)
    if not title:
        for block in blocks:
            if block["type"] == "paragraph":
                title = "".join(n.get("text", "") for n in block.get("content") or [])
                title = title.strip()[:60]
                if title:
                    break
    return (title or "未命名页面"), {"type": "doc", "content": blocks}


def build_pages(data: bytes) -> tuple[list[tuple[str, dict]], dict[str, tuple[bytes, str]]]:
    """MHT 字节 → ([(标题, doc)], 图片字典)。"""
    html_text, images = parse_mht(data)
    pages = [page_to_doc(page) for page in split_pages(html_text)]
    return pages, images
