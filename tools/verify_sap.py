# -*- coding: utf-8 -*-
"""SAP 启动链路回归验收（A 系统标识 / B1 窗口置前 / B2 启动校验 / 路径可配置）。

用法：py -3 tools\\verify_sap.py

自带后端进程 + 临时数据库，不污染真实 data/onlyone.db。
会真实拉起一次 SAP Logon 登录框（DS4 客户端 100），结束后关闭本次新开的窗口。
依赖：本机已安装 SAP GUI，且 SAP Logon 中存在系统标识 DS4 的客户端 100。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

TOKEN = "sap-verify-token-20261113"
PREFIX = "SAP验证-"


def http(base: str, path: str, method: str = "GET", body=None, expect_error: bool = False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        if expect_error:
            return exc.code, payload
        raise AssertionError("%s %s -> HTTP %s %s" % (method, path, exc.code, payload)) from exc


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def close_new_windows(sap_mod, before: set[int]) -> int:
    """只关本次回归新开的窗口，不碰用户原有的 SAP Logon 与已登录会话。"""
    closed = 0
    for win in sap_mod.sap_windows():
        if win["hwnd"] in before:
            continue
        if (win.get("title") or "").startswith("SAP Logon"):
            continue  # 主窗口不是本次产物
        if win["proc"] not in {"sapgui.exe", "saplogon.exe", "sapshcut.exe"}:
            continue
        try:
            ctypes.windll.user32.PostMessageW(win["hwnd"], 0x0010, 0, 0)  # WM_CLOSE
            closed += 1
        except Exception:  # noqa: BLE001
            pass
    return closed


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="onlyone_sap_verify_") as tmp:
        env = dict(os.environ)
        env["ONLYONE_TOKEN"] = TOKEN
        env["ONLYONE_DATA_DIR"] = tmp
        os.environ["ONLYONE_DATA_DIR"] = tmp  # 主进程直接 import 时同样用临时库

        from fastapi import HTTPException
        from onlyone import sap as sap_mod

        before = {w["hwnd"] for w in sap_mod.sap_windows()}
        if before:
            print("NOTE 已有 %d 个 SAP 窗口，收尾只关闭本次新增的" % len(before),
                  flush=True)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, str(BACKEND / "run.py"), "--no-window", "--no-watcher",
             "--port", str(port)],
            cwd=str(BACKEND), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % port
        results: list[bool] = []
        cid = None
        try:
            for _ in range(160):
                try:
                    with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(0.15)
            else:
                raise RuntimeError("后端未就绪，检查 data/onlyone.log")

            # --- 路径配置与自动探测 ---
            _, cfg = http(base, "/api/sap/config")
            results.append(check("自动探测到 SAP GUI 安装目录",
                                 bool(cfg["detected"]), cfg["sap_gui_dir"]))
            results.append(check("saplogon/sapshcut/Landscape 三文件齐全",
                                 all(cfg["files"].values()), str(cfg["files"])))
            results.append(check("Landscape 解析出连接项",
                                 cfg["systems"] > 0, "count=%s" % cfg["systems"]))

            _, systems = http(base, "/api/sap/systems")
            item = next((s for s in systems if s["key"] == "DS4|100"), None)
            results.append(check("A：DS4|100 的启动用标识是 SID=DS4（连接项名仅展示）",
                                 bool(item and item.get("launch_system") == "DS4"
                                      and item.get("launch_name") == "DS4-100ZH"),
                                 str(item)))

            code, _ = http(base, "/api/sap/config", "PUT",
                           {"sap_gui_dir": str(ROOT), "landscape_xml": ""},
                           expect_error=True)
            results.append(check("无 saplogon.exe 的目录被拒绝 (400)", code == 400,
                                 "code=%s" % code))
            _, saved = http(base, "/api/sap/config", "PUT",
                            {"sap_gui_dir": cfg["sap_gui_dir"], "landscape_xml": ""})
            results.append(check("手动指定目录可保存",
                                 saved["configured_gui_dir"] == cfg["sap_gui_dir"]))
            _, restored = http(base, "/api/sap/config", "PUT",
                               {"sap_gui_dir": "", "landscape_xml": ""})
            results.append(check("清空配置后恢复自动探测",
                                 restored["configured_gui_dir"] == ""
                                 and restored["detected"]))

            # --- 真实启动：A + B1 + B2 成功路径 ---
            _, cred = http(base, "/api/credentials", "POST",
                           {"title": PREFIX + "DS4-100", "username": "10000265",
                            "password": "", "sap_conn": "DS4|100"})
            cid = cred["id"]
            _, launched = http(base, "/api/credentials/%d/open-sap" % cid, "POST")
            results.append(check("A：按绑定启动解析出系统 SID",
                                 launched.get("launch_name") == "DS4",
                                 "launch_name=%s source=%s" % (
                                     launched.get("launch_name"), launched.get("source"))))
            ok = launched.get("verified") is True
            if not ok and "复用" in str(launched.get("detail", "")):
                print("NOTE 复用了已有会话窗口，verified=false 属预期降级", flush=True)
                ok = True
            results.append(check("B2：确认出现 SAP Logon 登录框", ok,
                                 "verified=%s front=%s" % (launched.get("verified"),
                                                           launched.get("front"))))
            results.append(check("B1：登录框已尝试置前",
                                 "front" in launched, "front=%s" % launched.get("front")))

            _, logon = http(base, "/api/open-sap", "POST")
            results.append(check("B1：SAP Logon 复用/启动且已置前",
                                 logon.get("verified") is True,
                                 "reused=%s front=%s" % (logon.get("reused"),
                                                         logon.get("front"))))

            # --- 失败路径 1：既没登录框也没对话框（sapshcut 退出 0 但无窗口）---
            class FakeProc:
                def poll(self):
                    return 0

            with patch.object(sap_mod.subprocess, "Popen", return_value=FakeProc()), \
                 patch.object(sap_mod, "_wait_sap_ready", return_value=(None, None)), \
                 patch.object(sap_mod, "sap_windows", return_value=[]):
                try:
                    sap_mod.launch_connection("DS4|100", "")
                    failed_loudly = False
                except HTTPException as exc:
                    failed_loudly = exc.status_code == 502
            results.append(check("B2：静默失败返回 502 明确错误而非假成功",
                                 failed_loudly))

            # --- 失败路径 2：GUI 弹属性对话框（即「部分数据不可用」）---
            with patch.object(sap_mod.subprocess, "Popen", return_value=FakeProc()), \
                 patch.object(sap_mod, "_wait_sap_ready",
                              return_value=(None, {"title": "SAP 快捷方式属性"})), \
                 patch.object(sap_mod, "sap_windows", return_value=[]):
                try:
                    sap_mod.launch_connection("DS4|100", "")
                    dialog_code, dialog_detail = None, ""
                except HTTPException as exc:
                    dialog_code, dialog_detail = exc.status_code, str(exc.detail)
            results.append(check("B2：属性对话框被识别为失败并回报标题",
                                 dialog_code == 502 and "快捷方式属性" in dialog_detail,
                                 "code=%s %s" % (dialog_code, dialog_detail[:40])))

            # --- A：启动参数形态（-system 只传 SID，传连接项名会 LoadAll 516）---
            captured: dict = {}

            def fake_popen(argv, *a, **kw):
                captured["argv"] = list(argv)
                return FakeProc()

            with patch.object(sap_mod.subprocess, "Popen", side_effect=fake_popen), \
                 patch.object(sap_mod, "_wait_sap_ready", return_value=(None, None)), \
                 patch.object(sap_mod, "sap_windows", return_value=[]):
                try:
                    sap_mod.launch_connection("DS4|120", "10000265")
                except HTTPException:
                    pass
            argv = captured.get("argv", [])
            results.append(check("A：-system 只传 SID，不含连接项名",
                                 "-system=DS4" in argv
                                 and not any("DS4-120ZH" in a for a in argv),
                                 " ".join(argv)))
            results.append(check("A：-client 取自凭据绑定",
                                 "-client=120" in argv, " ".join(argv)))
            results.append(check("A：-user 带用户名且不带 -pw（A 档）",
                                 "-user=10000265" in argv
                                 and not any(a.lower().startswith("-pw") for a in argv),
                                 " ".join(argv)))

            # --- 窗口识别：登录框算成功，属性框算失败 ---
            results.append(check("B2：Logon 登录框识别为成功标志",
                                 sap_mod._is_login_prompt(
                                     {"proc": "saplogon.exe",
                                      "title": "SAP GUI 快捷方式 - 登录 (DS4, 120, ZH, )"})
                                 and not sap_mod._is_login_prompt(
                                     {"proc": "saplogon.exe", "title": "SAP Logon 800"})))
            results.append(check("B2：属性对话框不计为会话/登录窗口",
                                 not sap_mod._is_session_window(
                                     {"proc": "sapgui.exe", "title": "SAP 快捷方式属性"})
                                 and sap_mod._is_error_dialog(
                                     {"proc": "saplogon.exe", "title": "系统条目属性"})))

            # --- 绑定值解析：SID|客户端 与 连接项名 都要落到 SID ---
            r1 = sap_mod.resolve_launch("DS4|120")
            r2 = sap_mod.resolve_launch("DS4-120ZH")
            results.append(check("A：DS4|120 解析为 SID=DS4 / client=120",
                                 r1["sid"] == "DS4" and r1["client"] == "120", str(r1)))
            results.append(check("A：连接项名 DS4-120ZH 反查为 SID=DS4",
                                 r2["sid"] == "DS4" and r2["client"] == "120", str(r2)))
            results.append(check("A：QS4|100 解析为 SID=QS4",
                                 sap_mod.resolve_launch("QS4|100")["sid"] == "QS4"))

            # --- 目录选择框：模拟用户选目录，不弹真实窗口 ---
            with patch("tkinter.Tk"), \
                 patch("tkinter.filedialog.askdirectory",
                       return_value=cfg["sap_gui_dir"]):
                picked = sap_mod.pick_dir()
            results.append(check("选择文件夹返回并校验通过",
                                 picked.get("path") == cfg["sap_gui_dir"],
                                 str(picked)))
            with patch("tkinter.Tk"), \
                 patch("tkinter.filedialog.askdirectory", return_value=str(ROOT)):
                picked_bad = sap_mod.pick_dir()
            results.append(check("选错目录时给出明确提示",
                                 bool(picked_bad.get("error")),
                                 str(picked_bad.get("error", ""))))
        finally:
            if cid is not None:
                try:
                    http(base, "/api/credentials/%d" % cid, "DELETE")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.4)
            closed = close_new_windows(sap_mod, before)
            if closed:
                print("CLEAN 关闭本次新开的 SAP 会话窗口 %d 个" % closed, flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    passed = sum(1 for r in results if r)
    print("\nRESULT %d/%d passed" % (passed, len(results)), flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
