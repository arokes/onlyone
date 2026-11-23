# -*- coding: utf-8 -*-
"""桌面壳桥接：让 HTTP 路由能弹出系统「另存为」对话框。

设计约束（2026-11-20）：

* 导出原先走前端 ``<a download>``。打包版是 pywebview，``settings['ALLOW_DOWNLOADS']``
  默认 **False**，WebView2 会直接丢弃下载请求——用户看到的是「点了没反应」，
  也不知道文件落在了哪里。于是改为后端写盘 + 系统另存为对话框。
* 这里只保存一个回调，不 import webview，便于 ``--no-window`` / 浏览器模式与单测。
  ``run.py`` 建好窗口后注册；没有注册时 ``ask_save_path`` 返回 None，调用方回退到 HTTP 下载。
"""
from __future__ import annotations

import threading
from typing import Callable

SaveDialog = Callable[[str], "str | None"]

_save_dialog: SaveDialog | None = None
# 对话框是模态的，同一时刻只允许一个，避免连点导出叠出多个窗口
_lock = threading.Lock()


def set_save_dialog(fn: SaveDialog | None) -> None:
    global _save_dialog
    _save_dialog = fn


def has_save_dialog() -> bool:
    return _save_dialog is not None


def ask_save_path(suggested_name: str) -> str | None:
    """弹出另存为对话框并返回用户选择的完整路径；用户取消返回 None。

    没有桌面壳（浏览器模式 / --no-window）时同样返回 None，由调用方回退。
    """
    fn = _save_dialog
    if fn is None:
        return None
    with _lock:
        return fn(suggested_name)
