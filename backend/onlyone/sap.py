# -*- coding: utf-8 -*-
"""SAP GUI 启动、路径配置与窗口校验（2026-11-13 适配 SAP GUI 8.00）。

背景：SAP GUI 8.00 用 SAPUILandscape.xml 把「系统定义」(type=SAPGUI) 与
「可登录连接项」(type=Reference) 拆开。sapshcut.exe 的 ``-system=`` 现在需要
连接项 name（如 DS4-100ZH）；传 SID（DS4）会退出 0 却不打开任何窗口。

职责：
* 配置持久化（app_meta）与自动探测：安装目录 / Landscape 文件都不写死唯一路径；
* Landscape 只读解析，供凭据绑定下拉使用；
* 启动 SAP Logon / 按连接项启动会话；
* Windows 窗口枚举 + 置前（B1）；
* 启动后校验会话窗口是否真的出现（B2），静默失败不再被记成「已启动」。
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path

from fastapi import HTTPException

from .db import connect

log = logging.getLogger("onlyone")

CFG_GUI_DIR = "sap_gui_dir"
CFG_LANDSCAPE = "sap_landscape_xml"

SAPLOGON_EXE = "saplogon.exe"
SAPSHCUT_EXE = "sapshcut.exe"

_WIN = sys.platform == "win32"


# ---------------------------------------------------------------- 配置存取
def _read_meta(key: str) -> str:
    """app_meta 是项目既有的 kv 表；未建库时静默按空值处理。"""
    try:
        conn = connect()
    except (OSError, sqlite3.Error):
        return ""
    try:
        row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def _write_meta(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 路径探测
def _candidate_gui_dirs() -> list[Path]:
    """按优先级列出候选安装目录（只读探测，不改系统配置）。

    顺序：用户配置 → 环境变量 → PATH → Program Files 下 SAP\\FrontEnd 的子目录。
    最后一项使升级后目录名变化（SAPgui → 其他名字）仍能自动识别。
    """
    out: list[Path] = []

    def add(raw) -> None:
        if not raw:
            return
        try:
            path = Path(str(raw).strip().strip('"')).expanduser()
            if path not in out:
                out.append(path)
        except (TypeError, ValueError, OSError):
            pass

    add(_read_meta(CFG_GUI_DIR))
    for env in ("ONLYONE_SAP_GUI_DIR", "SAP_GUI_DIR"):
        add(os.environ.get(env, ""))
    found = shutil.which(SAPLOGON_EXE)
    if found:
        add(Path(found).parent)
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        root = os.environ.get(env, "").strip()
        if not root:
            continue
        frontend = Path(root) / "SAP" / "FrontEnd"
        add(frontend / "SAPgui")
        try:
            for child in sorted(frontend.iterdir()):
                if child.is_dir():
                    add(child)
        except OSError:
            pass
    return out


def _gui_dir_valid(path: Path) -> bool:
    return (path / SAPLOGON_EXE).is_file()


def resolve_gui_dir() -> Path | None:
    for path in _candidate_gui_dirs():
        if _gui_dir_valid(path):
            return path
    return None


def resolve_landscape() -> Path:
    configured = _read_meta(CFG_LANDSCAPE).strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("APPDATA", "")) / "SAP" / "Common" / "SAPUILandscape.xml"


def validate_gui_dir(raw: str) -> tuple[str, str]:
    """返回 (规范化路径, 错误文本)；空串表示恢复自动探测。"""
    text = (raw or "").strip().strip('"')
    if not text:
        return "", ""
    path = Path(text).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return "", "目录不存在或不是绝对路径：%s" % text
    if not (path / SAPLOGON_EXE).is_file():
        return "", ("该目录下没有 saplogon.exe，请选择 SAP GUI 安装目录"
                    "（通常为 …\\SAP\\FrontEnd\\SAPgui）：%s" % text)
    return str(path), ""


def validate_landscape(raw: str) -> tuple[str, str]:
    text = (raw or "").strip().strip('"')
    if not text:
        return "", ""
    path = Path(text).expanduser()
    if not path.is_absolute() or not path.is_file():
        return "", "Landscape 文件不存在或不是绝对路径：%s" % text
    return str(path), ""


def get_config() -> dict:
    gui = resolve_gui_dir()
    land = resolve_landscape()
    return {
        "sap_gui_dir": str(gui) if gui else "",
        "configured_gui_dir": _read_meta(CFG_GUI_DIR).strip(),
        "landscape_xml": str(land),
        "configured_landscape_xml": _read_meta(CFG_LANDSCAPE).strip(),
        "detected": gui is not None,
        "files": {
            "saplogon": bool(gui and (gui / SAPLOGON_EXE).is_file()),
            "sapshcut": bool(gui and (gui / SAPSHCUT_EXE).is_file()),
            "landscape": land.is_file(),
        },
        "systems": len(list_systems()),
    }


def save_config(gui_dir: str | None = None, landscape_xml: str | None = None) -> dict:
    """None 表示该字段保持原值；空串表示清除并恢复自动探测。"""
    if gui_dir is not None:
        value, err = validate_gui_dir(gui_dir)
        if err:
            raise HTTPException(400, err)
        _write_meta(CFG_GUI_DIR, value)
    if landscape_xml is not None:
        value, err = validate_landscape(landscape_xml)
        if err:
            raise HTTPException(400, err)
        _write_meta(CFG_LANDSCAPE, value)
    log.info("SAP 路径配置已更新 gui_dir=%s landscape=%s",
             _read_meta(CFG_GUI_DIR) or "(自动探测)",
             _read_meta(CFG_LANDSCAPE) or "(自动探测)")
    return get_config()


def pick_dir() -> dict:
    """弹出原生文件夹选择框；不可用时返回原因，前端仍可手动粘贴路径。"""
    if not _WIN:
        return {"cancelled": True, "error": "仅 Windows 支持文件夹选择"}
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001 - 部分窗口管理器不支持该属性
            pass
        try:
            chosen = filedialog.askdirectory(
                parent=root, title="选择 SAP GUI 安装目录（包含 saplogon.exe）")
        finally:
            root.destroy()
        if not chosen:
            return {"cancelled": True}
        path, err = validate_gui_dir(chosen)
        if err:
            return {"cancelled": False, "error": err}
        return {"cancelled": False, "path": path}
    except Exception as exc:  # noqa: BLE001 - 目录选择是可选入口，不阻断主流程
        log.warning("SAP 目录选择框不可用：%s", exc)
        return {"cancelled": True,
                "error": "无法打开文件夹选择框（%s），请手动粘贴路径" % exc}


# ---------------------------------------------------------------- Landscape
def _services() -> list[dict]:
    """只读解析 SAPUILandscape.xml，不修改公司配置。"""
    path = resolve_landscape()
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        log.warning("SAP Landscape 解析失败：%s (%s)", exc, path)
        return []
    out: list[dict] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Service":
            continue
        a = {k.lower(): v for k, v in elem.attrib.items()}
        if not a.get("name"):
            continue
        out.append({
            "type": a.get("type") or "",
            "name": a.get("name") or "",
            "sid": (a.get("systemid") or "").strip(),
            "client": (a.get("client") or "").strip(),
            "user": a.get("user") or "",
            "language": a.get("language") or "",
        })
    return out


def list_systems() -> list[dict]:
    """只列可登录连接项（type=Reference）。key 沿用 SID|客户端，兼容既有凭据。"""
    out: list[dict] = []
    seen: set[str] = set()
    for svc in _services():
        if svc["type"].lower() != "reference" or not svc["sid"]:
            continue
        key = f"{svc['sid']}|{svc['client']}" if svc["client"] else svc["sid"]
        if key in seen:
            continue
        seen.add(key)
        base = (f"{svc['sid']} · 客户端 {svc['client']}" if svc["client"]
                else svc["sid"])
        out.append({
            "key": key, "label": f"{base}（{svc['name']}）",
            "sid": svc["sid"], "client": svc["client"],
            "name": svc["name"],
            # 实际传给 sapshcut 的是系统标识；连接项名仅供参考展示
            "launch_system": svc["sid"], "launch_name": svc["name"],
        })
    return out


def resolve_launch(sap_conn: str) -> dict:
    """把凭据绑定值翻译成 sapshcut 需要的系统标识/客户端/语言。

    **``-system=`` 必须是系统标识（SID，如 DS4/QS4），绝不能传连接项名（DS4-120ZH）。**
    2026-11-18 15:32 的 ``sapshlib_*.trc`` 再次坐实：传连接项名时 sapshlib 打印
    ``Getting Description according to System ID in SAP landscape file >DS4-120ZH<`` →
    ``Error in LoadAll: code = 516, Not all system data are available``，
    对应界面上那个「部分数据不可用」错误框（标题恰为 ``SAP GUI``，很容易被误判成
    「已打开会话」的假成功）。传 ``DS4`` + ``-client=120`` 才是正确形态。

    注意：传 SID 时若该用户已有同系统会话，SAP 会**复用**而不新开窗口（exit=0、
    不写 trace），这不是失败——调用方应按「复用」处理，见 ``launch_connection``。
    """
    conn = (sap_conn or "").strip()
    sid, _, client = conn.partition("|")
    sid, client = sid.strip(), client.strip()
    if not sid:
        raise HTTPException(400, "该凭据未绑定 SAP 系统")
    refs = [s for s in _services() if s["type"].lower() == "reference"]
    # 1) 绑定值是连接项名（如 DS4-120ZH）→ 反查它的 systemid
    for svc in refs:
        if svc["name"].lower() == conn.lower():
            return {"sid": svc["sid"], "client": client or svc["client"],
                    "language": svc["language"] or "ZH",
                    "source": "连接项名 → 系统标识"}
    # 2) 常规：SID|客户端 → 直接用 SID，客户端取自绑定值
    for svc in refs:
        if svc["sid"].lower() == sid.lower() and (not client or svc["client"] == client):
            return {"sid": svc["sid"], "client": client or svc["client"],
                    "language": svc["language"] or "ZH", "source": "系统标识"}
    # 3) Landscape 里没这个 SID：仍按 SID 传，由 GUI 的报错对话框暴露出来
    log.warning("SAP 系统标识未在 Landscape 中匹配：key=%s", conn)
    return {"sid": sid, "client": client, "language": "ZH", "source": "未匹配"}


# ---------------------------------------------------------------- Win32 窗口
if _WIN:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                    wintypes.DWORD, ctypes.c_size_t]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_SAP_PROCS = {"saplogon.exe", "sapgui.exe", "sapshcut.exe"}


def _proc_name(pid: int) -> str:
    if not _WIN or not pid:
        return ""
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def sap_windows(proc: str | None = None) -> list[dict]:
    """枚举当前可见的 SAP 顶层窗口（按进程名识别，标题不依赖版本号）。"""
    if not _WIN:
        return []
    found: list[dict] = []

    def callback(hwnd, _lparam):
        if not hwnd or not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = _proc_name(pid.value)
        if name in _SAP_PROCS and (proc is None or name == proc):
            found.append({"hwnd": int(hwnd), "pid": pid.value,
                          "proc": name, "title": buf.value})
        return True

    _user32.EnumWindows(_ENUM_PROC(callback), 0)
    return found


def bring_to_front(hwnd: int) -> bool:
    """B1：把 SAP 窗口恢复并置前。

    后台进程调用 SetForegroundWindow 会受 Windows 前台锁定限制，
    先模拟一次 ALT 按键解除锁定；失败时仅记日志，不影响启动结果。
    """
    if not _WIN or not hwnd:
        return False
    try:
        target = wintypes.HWND(hwnd)
        _user32.ShowWindow(target, 9)  # SW_RESTORE
        _user32.keybd_event(0x12, 0, 0, 0)      # VK_MENU down
        _user32.keybd_event(0x12, 0, 2, 0)      # KEYEVENTF_KEYUP
        ok = bool(_user32.SetForegroundWindow(target))
        _user32.BringWindowToTop(target)
        return ok
    except Exception as exc:  # noqa: BLE001
        log.warning("SAP 窗口置前失败：%s", exc)
        return False


# A 档（不传 -pw）下，sapshcut 的成功标志是 SAP Logon 弹出的
# 「SAP GUI 快捷方式 - 登录 (SID, 客户端, 语言, 事务)」对话框：用户名已填、密码待输。
_LOGIN_TITLE_MARK = "SAP GUI 快捷方式 - 登录"
# 参数不被接受时 GUI 改弹属性对话框（trace 对应 LoadAll code=516），
# 二者与会话窗口同进程、同可见性，只能靠标题区分。
_ERROR_DIALOG_TITLES = ("SAP 快捷方式属性", "系统条目属性")


def _is_login_prompt(win: dict) -> bool:
    """「SAP GUI 快捷方式 - 登录」框 = 启动成功、等待输入密码。

    只认标题不认进程：SAP GUI 7.70 起是单进程架构，登录框与会话窗口都挂在
    ``saplogon.exe`` 下；更老的版本才单独有 ``sapgui.exe``。
    """
    return _LOGIN_TITLE_MARK in (win.get("title") or "")


def _is_error_dialog(win: dict) -> bool:
    title = win.get("title") or ""
    return any(mark in title for mark in _ERROR_DIALOG_TITLES)


def _is_logon_window(win: dict) -> bool:
    """SAP Logon 的主界面（连接列表），既不是会话窗口也不是登录框。"""
    return ((win.get("title") or "").startswith("SAP Logon")
            and win.get("proc") in ("saplogon.exe", "sapgui.exe"))


def _is_session_window(win: dict) -> bool:
    """已登录的 SAP 会话窗口（复用场景）；排除 Logon 主界面、登录框与报错框。

    2026-11-18：原实现只认 ``sapgui.exe``，而单进程架构下会话窗口是
    ``saplogon.exe``（标题如「更改物料 (初始屏幕)」），导致复用会话永远识别不到、
    启动被判失败。改为按「不是主界面 / 不是登录框 / 不是报错框」反选。
    """
    if win.get("proc") not in ("saplogon.exe", "sapgui.exe"):
        return False
    title = (win.get("title") or "").strip()
    if not title or title == "SAP GUI":
        # 「SAP GUI」是加载中与错误占位的通用标题。传错 -system 时 sapshlib 会弹
        # 「部分数据不可用」错误框，标题也是它——曾据此判成 verified=1 的假成功。
        return False
    if _is_error_dialog(win) or _is_logon_window(win) or _is_login_prompt(win):
        return False
    return True


def _wait_sap_ready(before: set[int], timeout: float = 10.0):
    """等待登录框（成功）或报错对话框（失败），返回 (登录框, 错误框)。

    超时返回 (None, None)，由调用方区分「复用已有会话」与「彻底没打开」。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for win in sap_windows():
            if win["hwnd"] in before:
                continue
            if _is_error_dialog(win):
                return None, win
            if _is_login_prompt(win) or _is_session_window(win):
                time.sleep(0.5)  # 排除一闪而过的假窗口
                alive = [w for w in sap_windows() if w["hwnd"] == win["hwnd"]]
                return (alive[0] if alive else None), None
        time.sleep(0.25)
    return None, None


# ---------------------------------------------------------------- 启动
def _require_gui() -> Path:
    gui = resolve_gui_dir()
    if gui is None:
        raise HTTPException(
            404, "未找到 SAP GUI 安装目录（saplogon.exe）。"
                 "请在「凭据库 → SAP 连接设置」里选择安装目录。")
    return gui


def launch_logon() -> dict:
    """启动 SAP Logon；已在运行时不再重复启动，直接把窗口置前。"""
    gui = _require_gui()
    existing = [w for w in sap_windows() if _is_logon_window(w)]
    if existing:
        front = bring_to_front(existing[0]["hwnd"])
        log.info("SAP Logon 已在运行，窗口置前 result=%s dir=%s", front, gui)
        return {"opened": "saplogon", "reused": True, "verified": True,
                "front": front, "gui_dir": str(gui)}
    try:
        subprocess.Popen([str(gui / SAPLOGON_EXE)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "启动失败：%s" % exc) from exc
    deadline = time.monotonic() + 4.0
    win = None
    while time.monotonic() < deadline:
        wins = [w for w in sap_windows() if _is_logon_window(w)]
        if wins:
            win = wins[0]
            break
        time.sleep(0.2)
    if win is None:
        log.error("SAP Logon 启动后未出现窗口 dir=%s", gui)
        raise HTTPException(502, "saplogon.exe 已启动但未出现 SAP Logon 窗口，"
                                 "请核对安装目录或手动启动一次 SAP Logon。")
    front = bring_to_front(win["hwnd"])
    log.info("已启动 SAP Logon dir=%s front=%s", gui, front)
    return {"opened": "saplogon", "reused": False, "verified": True,
            "front": front, "gui_dir": str(gui)}


def launch_connection(sap_conn: str, username: str) -> dict:
    """A 档：只带 -user，绝不传 -pw；密码由前端复制后手动粘贴。"""
    gui = _require_gui()
    shcut = gui / SAPSHCUT_EXE
    if not shcut.is_file():
        raise HTTPException(404, "未在 %s 找到 sapshcut.exe" % gui)

    plan = resolve_launch(sap_conn)
    sid, client, language, source = (plan["sid"], plan["client"],
                                     plan["language"], plan["source"])
    # -system= 只认 SID（DS4）：传连接项名 DS4-120ZH 会让 sapshlib LoadAll 516，
    # 弹出「部分数据不可用」错误框（标题恰为 SAP GUI，极易被误认为已打开会话）。
    argv = [str(shcut), "-type=System", "-system=" + sid]
    if client.strip():
        argv.append("-client=" + client.strip())
    if (username or "").strip():
        argv.append("-user=" + username.strip())
    argv.append("-language=" + language)

    before = {w["hwnd"] for w in sap_windows()}
    try:
        proc = subprocess.Popen(argv)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "启动失败：%s" % exc) from exc

    common = {
        "opened": sap_conn, "launch_name": sid, "client": client,
        "source": source,
        "username_filled": bool((username or "").strip()),
        "password_filled": False, "gui_dir": str(gui),
    }
    ready, dialog = _wait_sap_ready(before)
    if dialog is not None:
        log.error("SAP GUI 拒绝启动参数 key=%s -system=%s client=%s 对话框=%r",
                  sap_conn, sid, client, dialog["title"])
        raise HTTPException(
            502,
            "SAP GUI 弹出了「%s」对话框，说明启动参数未被接受（-system=%s，客户端 %s）。"
            "请确认 SAP Logon 中存在对应连接项，且客户端号有效。"
            % (dialog["title"], sid, client or "未指定"))
    if ready is not None:
        front = bring_to_front(ready["hwnd"])
        log.info("已按绑定启动 SAP 连接 key=%s → -system=%s -client=%s（%s）"
                 "verified=1 front=%s 窗口=%r",
                 sap_conn, sid, client, source, front, ready["title"])
        return {**common, "verified": True, "front": front, "detail": ""}

    # 超时：区分「复用已有会话」与「彻底没打开」
    exit_code = proc.poll()
    existing = [w for w in sap_windows() if _is_session_window(w)]
    if existing:
        front = bring_to_front(existing[0]["hwnd"])
        detail = ("SAP GUI 已有登录会话，已复用当前会话并置前，未新建连接。"
                  "（本次启动参数 -system=%s，sapshcut 退出码 %s；"
                  "若需新开会话，请在 SAP Logon 里手动打开对应连接项。）"
                  % (sid, exit_code))
        log.warning("SAP 启动结果未验证 key=%s -system=%s exit=%s detail=%s",
                    sap_conn, sid, exit_code, detail)
        return {**common, "verified": False, "front": front, "detail": detail}

    log.error("SAP 启动未出现登录窗口 key=%s -system=%s client=%s exit=%s gui=%s",
              sap_conn, sid, client, exit_code, gui)
    raise HTTPException(
        502,
        "SAP GUI 未出现登录窗口（sapshcut 退出码 %s）。当前启动项 -system=%s "
        "-client=%s（%s）。请确认 SAP Logon 中存在该连接项 %s。" % (
            exit_code if exit_code is not None else "运行中", sid,
            client or "未指定", source, sid))
