# -*- coding: utf-8 -*-
"""全局热键（2026-11-25，功能 6）：Ctrl+Alt+Space 唤起快速捕获。

实现方式
--------
pywin32 的 `RegisterHotKey` 必须绑定在一个**有消息循环的窗口**上，而 pywebview 的窗口
由它自己的 UI 线程持有，不能直接注册。因此这里在后台线程里创建一个 0 尺寸的隐藏窗口，
专用于接收 `WM_HOTKEY`，收到后只做两件事：

1. 把「待打开」标志与触发前的**前台窗口句柄**记下来（供前端轮询与保存后回焦）；
2. 调用回调把主窗口置前（回调由 run.py 注入，避免本模块依赖 pywebview）。

降级：没有 pywin32、注册失败（被别的程序占用）或设置了 `ONLYONE_HOTKEY_OFF=1` 时，
只记一条日志，不影响应用启动——热键是增强，不是必需。

默认组合 Ctrl+Alt+Space：**不要用 Alt+Space**，那是 Windows 的系统窗口菜单快捷键。
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("onlyone")

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_SPACE = 0x20
HOTKEY_ID = 0xB001

DEFAULT_HOTKEY = "ctrl+alt+space"

_state = {"pending": False, "fore": 0, "enabled": False,
          "error": "", "hotkey": DEFAULT_HOTKEY}
_lock = threading.Lock()
_thread: threading.Thread | None = None
_thread_id: int | None = None
_hwnd = None


def _parse_hotkey(text: str) -> tuple[int, int]:
    """``ctrl+alt+k`` → (modifiers, vk)。不认识的写法直接抛 ValueError。"""
    mods = 0
    vk = None
    for part in (text or "").lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "meta"):
            mods |= MOD_WIN
        elif part == "space":
            vk = VK_SPACE
        elif len(part) == 1:
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError("无法识别的按键：%s" % part)
    if vk is None or mods == 0:
        raise ValueError("需要至少一个修饰键和一个主键")
    return mods, vk


def consume() -> tuple[bool, int]:
    """前端轮询调用：取走「待打开」标志（只触发一次）。"""
    with _lock:
        opened = _state["pending"]
        fore = _state["fore"]
        _state["pending"] = False
    return opened, fore


def poke(fore: int = 0) -> None:
    """开发/自动化专用：模拟一次热键触发（生产接口不暴露，见 capture._trigger）。"""
    with _lock:
        _state["pending"] = True
        _state["fore"] = fore


def restore_foreground(hwnd: int | None) -> None:
    """保存/取消后把焦点还给触发前的窗口。失败静默：抢焦点本就受系统策略限制。"""
    if not hwnd:
        return
    try:
        import win32con
        import win32gui
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:  # noqa: BLE001
        log.debug("恢复前台窗口失败 hwnd=%s：%s", hwnd, exc)


def status() -> dict:
    with _lock:
        return dict(_state)


def _run(on_trigger) -> None:
    global _hwnd, _thread_id
    try:
        import win32api
        import win32con
        import win32gui

        _thread_id = win32api.GetCurrentThreadId()

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY and wparam == HOTKEY_ID:
                try:
                    fore = win32gui.GetForegroundWindow()
                except Exception:  # noqa: BLE001
                    fore = 0
                with _lock:
                    _state["pending"] = True
                    _state["fore"] = fore
                try:
                    on_trigger()
                except Exception as exc:  # noqa: BLE001
                    log.warning("热键回调失败：%s", exc)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        hotkey_text = os.environ.get("ONLYONE_HOTKEY", DEFAULT_HOTKEY)
        mods, vk = _parse_hotkey(hotkey_text)
        with _lock:
            _state["hotkey"] = hotkey_text

        message_map = {WM_HOTKEY: wndproc}
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "OnlyOneHotkeyWindow"
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpfnWndProc = message_map
        atom = win32gui.RegisterClass(wc)
        hwnd = win32gui.CreateWindow(atom, "OnlyOneHotkey", 0, 0, 0, 0, 0, 0, 0,
                                     wc.hInstance, None)
        _hwnd = hwnd
        win32gui.RegisterHotKey(hwnd, HOTKEY_ID, mods, vk)
        with _lock:
            _state["enabled"] = True
            _state["error"] = ""
        log.info("全局热键已注册：%s", hotkey_text)
        win32gui.PumpMessages()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state["enabled"] = False
            _state["error"] = str(exc)
        hint = ""
        if "1409" in str(exc) or "热键已注册" in str(exc):
            hint = "：该组合已被另一个 OnlyOne 实例或输入法/Quicker 等软件占用"
        log.warning(
            "全局热键 %s 注册失败%s。可设 ONLYONE_HOTKEY（如 ctrl+alt+k）换一个组合，"
            "或设 ONLYONE_HOTKEY_OFF=1 关闭热键。原始错误：%s",
            os.environ.get("ONLYONE_HOTKEY", DEFAULT_HOTKEY), hint, exc)


def start(on_trigger) -> None:
    """在后台线程注册热键；重复调用只生效一次。"""
    global _thread
    if os.environ.get("ONLYONE_HOTKEY_OFF") == "1":
        log.info("ONLYONE_HOTKEY_OFF=1，跳过全局热键")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, args=(on_trigger,),
                               name="onlyone-hotkey", daemon=True)
    _thread.start()


def stop() -> None:
    if _thread_id is None:
        return
    try:
        import win32api
        import win32con
        win32api.PostThreadMessage(_thread_id, win32con.WM_QUIT, 0, 0)
    except Exception:  # noqa: BLE001
        pass
