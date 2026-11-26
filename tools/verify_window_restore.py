# -*- coding: utf-8 -*-
"""窗口收缩/恢复的实机回归（2026-11-25）。

用 tkinter 造一个真实 Win32 顶层窗口（标题与主窗口一致），最大化后走
``desktop.enter_capture`` → ``desktop.exit_capture``，检查：

1. 收缩后尺寸接近 620×270；
2. **恢复后仍处于最大化状态**（这正是用户两次反馈的点）；
3. 重复进入不会把 620×270 记成"原始尺寸"。

注意：这里验证的是 Win32 层面的行为。pywebview 的窗口状态是否同步，
仍需在打包后的 EXE 上人工按一次热键确认（G-24）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import tkinter as tk  # noqa: E402

from onlyone import desktop  # noqa: E402

TITLE = "OnlyOne - 个人工作台"
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("%s %s%s" % ("PASS " if ok else "FAIL ", label, ("  " + detail) if detail else "")),
          flush=True)
    results.append(bool(ok))


def pump(root: tk.Tk, seconds: float = 0.35) -> None:
    """跑一会儿事件循环，让窗口管理器把尺寸/状态真正应用下去。"""
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def main() -> int:
    import win32gui

    desktop.set_window_title(TITLE)
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("900x600+120+120")
    pump(root, 0.4)
    root.state("zoomed")
    pump(root, 0.6)

    hwnd = win32gui.FindWindow(None, TITLE)
    if not hwnd:
        print("FAIL 找不到测试窗口")
        return 1
    import win32con

    def is_maxed(h: int) -> bool:
        return win32gui.GetWindowPlacement(h)[1] == win32con.SW_SHOWMAXIMIZED

    check("窗口已最大化", is_maxed(hwnd), "showCmd=%s" % win32gui.GetWindowPlacement(hwnd)[1])

    started = desktop.enter_capture()
    pump(root, 0.5)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    check("进入捕获：窗口被收缩", started and (right - left) < 700 and (bottom - top) < 340,
          "%dx%d" % (right - left, bottom - top))
    check("进入捕获：不再是最大化", not is_maxed(hwnd))

    # 再按一次热键：不应覆盖已记录的原始几何
    desktop.enter_capture()
    pump(root, 0.3)

    desktop.exit_capture(0)
    pump(root, 0.7)
    restored_max = is_maxed(hwnd)
    left2, top2, right2, bottom2 = win32gui.GetWindowRect(hwnd)
    check("退出捕获：恢复最大化状态", restored_max,
          "showCmd=%s size=%dx%d" % (win32gui.GetWindowPlacement(hwnd)[1], right2 - left2, bottom2 - top2))
    check("退出捕获：不再是 620×270", (right2 - left2) > 700,
          "%dx%d" % (right2 - left2, bottom2 - top2))

    root.destroy()
    ok = all(results)
    print("\n%s  (%d/%d)" % ("ALL PASS" if ok else "SOME FAILED",
                             sum(results), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
