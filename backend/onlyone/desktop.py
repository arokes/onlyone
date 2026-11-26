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

import logging
import os
import threading
from typing import Callable

from . import hotkey

log = logging.getLogger("onlyone")

SaveDialog = Callable[[str], "str | None"]

_save_dialog: SaveDialog | None = None
# 对话框是模态的，同一时刻只允许一个，避免连点导出叠出多个窗口
_lock = threading.Lock()

# ---- 快速捕获的窗口收缩（2026-11-25）----
# 单窗口架构下，热键若只置前主窗口，用户看到的是整个工作台。
# 这里用 Win32 把主窗口临时缩成一张卡片，退出时按原几何恢复（含最大化状态）。
# 失败一律静默：宁可窗口不缩，也不能让热键或保存流程报错。
_title = "OnlyOne - 个人工作台"
# 独立捕获窗口（2026-11-25 二次改版）：主窗口不动，热键只弹这个无边框小窗。
_capture_window = None
_geometry: dict = {"hwnd": 0, "rect": None, "max": False, "placement": None}
CAPTURE_WIDTH = 620
CAPTURE_HEIGHT = 270


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


def set_capture_window(window) -> None:
    """注册 pywebview 建好的捕获小窗（hidden=True 创建，热键时 show）。"""
    global _capture_window
    _capture_window = window


def show_capture() -> bool:
    """热键回调：只显示独立小窗。

    用户要求「热键只调出小窗，不要调整主窗口」（2026-11-26），所以这里
    不隐藏、不最小化、不还原主窗口——它原来什么样，按完热键还是什么样。
    """
    window = _capture_window
    if window is None:
        log.warning("捕获小窗未创建，热键暂不响应")
        return False
    try:
        window.show()
        try:
            window.on_top = True
        except Exception:  # noqa: BLE001 - 个别版本没有该属性
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("显示捕获窗口失败：%s", exc)
        return False


def hide_capture() -> bool:
    """捕获完成后隐藏小窗；主窗口完全不受影响（原本最小化就还最小化）。"""
    window = _capture_window
    if window is None:
        return False
    try:
        window.hide()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("隐藏捕获窗口失败：%s", exc)
        return False


def finish_capture(fore: int | None = None) -> None:
    """一次捕获结束：隐藏独立小窗并回焦。

    即使隐藏失败也**不再**回退到「变形主窗口」——用户明确不要那个形态
    （2026-11-26）。不动主窗口。enter_capture / exit_capture 仅作为工具函数保留。
    """
    hide_capture()
    if fore:
        hotkey.restore_foreground(fore)


def set_window_title(title: str) -> None:
    """记录主窗口标题，供 FindWindow 定位（标题会临时改成「快速捕获」）。"""
    global _title
    _title = title or _title


def _hwnd() -> int:
    try:
        import win32gui
        return _geometry.get("hwnd") or win32gui.FindWindow(None, _title)
    except Exception:  # noqa: BLE001
        return 0


def enter_capture() -> bool:
    """把主窗口缩成捕获卡片并置前；返回是否真的缩了（供调用方决定后续行为）。"""
    if os.environ.get("ONLYONE_CAPTURE_SHRINK") == "0":
        return False
    try:
        import win32api
        import win32con
        import win32gui

        # 幂等：已经在捕获模式时只置前，绝不重新记录几何。
        # 否则连按两次热键会把「620×270」当成原始尺寸，退出时窗口就回不去了。
        if _geometry.get("hwnd"):
            try:
                win32gui.SetForegroundWindow(_geometry["hwnd"])
            except Exception:  # noqa: BLE001
                pass
            return True

        hwnd = _hwnd()
        if not hwnd:
            return False

        # 用 WINDOWPLACEMENT 整体记录：它同时带 showCmd（是否最大化）与
        # rcNormalPosition（还原后的矩形），比「GetWindowRect + IsZoomed」更完整。
        # 上一版只靠 rect + SW_MAXIMIZE，pywebview 的窗口状态与之不同步，
        # 结果保存后窗口停在还原尺寸（用户 2026-11-25 二次反馈）。
        placement = win32gui.GetWindowPlacement(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        # 注意：不要用 win32gui.IsZoomed —— 这个 API 在部分 pywin32 版本里不存在
        # （本机 306 就没有），会抛 AttributeError 并被外层 except 吞掉，
        # 表现成"热键毫无反应"。showCmd 才是跨版本可靠的判断。
        was_max = placement[1] == win32con.SW_SHOWMAXIMIZED
        _geometry.update(hwnd=hwnd, rect=rect, max=was_max, placement=placement)

        if was_max:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        screen_w = win32api.GetSystemMetrics(0)
        screen_h = win32api.GetSystemMetrics(1)
        x = max(0, (screen_w - CAPTURE_WIDTH) // 2)
        y = max(0, int(screen_h * 0.22))
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, x, y,
                              CAPTURE_WIDTH, CAPTURE_HEIGHT,
                              win32con.SWP_SHOWWINDOW | win32con.SWP_NOACTIVATE)
        win32gui.SetWindowText(hwnd, "OnlyOne 快速捕获")
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("收缩主窗口失败：%s", exc)
        return False


def exit_capture(fore: int | None = None) -> None:
    """恢复主窗口几何与标题，并把焦点还给触发前的窗口。"""
    try:
        import win32con
        import win32gui

        hwnd = _geometry.get("hwnd") or 0
        if hwnd:
            win32gui.SetWindowText(hwnd, _title)
            placement = _geometry.get("placement")
            if placement:
                # 一次把尺寸、位置、最大化状态都还原回去
                win32gui.SetWindowPlacement(hwnd, placement)
            elif _geometry.get("max"):
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            elif _geometry.get("rect"):
                left, top, right, bottom = _geometry["rect"]
                win32gui.SetWindowPos(hwnd, 0, left, top, right - left, bottom - top,
                                      win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        _geometry.update(hwnd=0, rect=None, max=False, placement=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("恢复主窗口失败：%s", exc)

    if fore:
        try:
            import win32con
            import win32gui
            win32gui.ShowWindow(fore, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(fore)
        except Exception:  # noqa: BLE001
            pass
