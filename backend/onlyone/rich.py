# -*- coding: utf-8 -*-
"""富文本字段的纯文本抽取（与记事本同一套 ProseMirror 结构）。

为什么单独成模块
----------------
运维记录的 ``symptom`` / ``root_cause`` / ``action`` / ``note`` 与记事本页面的
``content_json`` 存的是同一种结构（TipTap ``getJSON()`` 的 JSON 字符串），
都需要一条「JSON → 纯文本」的通道供 FTS / 条目标题 / 摘要使用。

在 ``rich`` 出现之前这段逻辑只在 ``notes._extract_text`` 里，业务记录这边若再
抄一份就有两处实现、两套行为。这里收成唯一实现，``notes`` 与 ``entries`` 共用。

兼容旧数据
----------
本次改动之前这几列存的是纯文本（无 JSON）。``parse_doc`` 对非法 JSON 直接
返回 ``None``，调用方按“本来就只是文本”处理——不做批量迁移，只在用户下次
编辑保存时自然转成 JSON。
"""
from __future__ import annotations

import json
from typing import Any

# 块级节点收尾补一个换行，保证正文段落之间不会粘成一行（与 notes 一致）
BLOCK_TYPES = {"paragraph", "heading", "codeBlock", "blockquote", "listItem",
               "bulletList", "orderedList", "table", "tableRow"}


def parse_doc(value: Any) -> dict | None:
    """把列值解析成 ProseMirror doc；不是合法 doc 时返回 ``None``。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None                     # 纯文本老数据：快速跳过 JSON 解析
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(doc, dict) and doc.get("type") == "doc":
        return doc
    return None


def doc_from_text(text: str) -> dict:
    """把纯文本包成段落文档（供编辑器载入老数据；不写库）。"""
    lines = (text or "").splitlines()
    content = [{"type": "paragraph", "content": [{"type": "text", "text": line}]}
               for line in lines if line.strip()]
    return {"type": "doc", "content": content or [{"type": "paragraph"}]}


def extract_text(value: Any) -> str:
    """富文本列 → 纯文本。纯文本老数据原样返回；图片节点不产生字符。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    doc = parse_doc(value)
    if doc is None:
        return value.strip()

    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "text":
            parts.append(str(node.get("text") or ""))
        elif kind == "hardBreak":
            parts.append("\n")
        # image 节点刻意不出字符：图片 URL 进 FTS 只会污染检索与摘要
        for child in node.get("content") or []:
            walk(child)
        if kind in BLOCK_TYPES:
            parts.append("\n")

    walk(doc.get("content") or [])
    return "".join(parts).strip()


def is_rich(value: Any) -> bool:
    """该列值是否已经是富文本文档（供前端 / 调试判断，不做迁移）。"""
    return parse_doc(value) is not None
