# -*- coding: utf-8 -*-
"""可插拔检索后端（M6-3）：默认关闭，检测到已安装才启用。

约束（v1.0 §1.3）：

* 不整合 A 路线现成应用的程序本体，只调用其 CLI；
* 未安装 / 未配置时行为与本地检索完全一致；
* 结果作为**独立分组** ``plugin_items`` 返回，不参与跨类型关联、不与本地 score 混排；
* 只读外部索引，不创建索引、不改动其配置。

启用方式：把 ``ONLYONE_KENSAKU_CLI`` 指向可执行的 CLI（.py 或 .exe）。
约定协议：``<cli> --query <q> --format json``，stdout 输出 JSON 数组
``[{"title": "...", "snippet": "...", "url": "..."}]``。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("onlyone")

CLI_ENV = "ONLYONE_KENSAKU_CLI"


class SearchBackend:
    name = "base"

    def available(self) -> bool:
        return False

    def search(self, query: str) -> list[dict]:
        return []


class KensakuBackend(SearchBackend):
    """可选的外部索引后端；只在环境变量指向真实文件时启用。"""

    name = "Kensaku"

    def __init__(self) -> None:
        self.cli = os.environ.get(CLI_ENV, "").strip()

    def available(self) -> bool:
        return bool(self.cli) and Path(self.cli).is_file()

    def search(self, query: str) -> list[dict]:
        argv = ([sys.executable, self.cli] if self.cli.lower().endswith(".py")
                else [self.cli])
        argv += ["--query", query, "--format", "json"]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                timeout=20, check=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("外部索引调用失败 %s: %s", self.name, exc)
            return []
        if proc.returncode != 0:
            log.warning("外部索引退出码 %s: %s", proc.returncode,
                        (proc.stderr or "")[:200])
            return []
        try:
            data = json.loads(proc.stdout or "[]")
        except (ValueError, TypeError):
            log.warning("外部索引返回的不是合法 JSON")
            return []
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for index, item in enumerate(data[:50]):
            if not isinstance(item, dict):
                continue
            out.append({
                # 负数占位，避免与本地 entry_id 混淆；前端不会用它做预览 / 关联
                "id": -1000 - index,
                "type": "plugin",
                "title": str(item.get("title") or "外部索引结果"),
                "snippet": str(item.get("snippet") or ""),
                "url": str(item.get("url") or ""),
                "source": self.name,
                "via": "plugin",
                "abs_path": None,
            })
        return out


BACKENDS: tuple[SearchBackend, ...] = (KensakuBackend(),)


def enabled_backends() -> list[SearchBackend]:
    return [backend for backend in BACKENDS if backend.available()]


def search_plugins(query: str) -> list[dict]:
    out: list[dict] = []
    for backend in enabled_backends():
        try:
            out.extend(backend.search(query))
        except Exception as exc:  # noqa: BLE001
            log.warning("外部索引 %s 异常：%s", backend.name, exc)
    return out


def status() -> dict:
    enabled = enabled_backends()
    return {
        "available": bool(enabled),
        "enabled": [{"name": b.name, "cli": getattr(b, "cli", "")} for b in enabled],
        "env": CLI_ENV,
    }
