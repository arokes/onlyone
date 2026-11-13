# -*- coding: utf-8 -*-
"""SAP 启动链路回归验收（A 连接项名 / B1 窗口置前 / B2 启动校验 / 路径可配置）。

用法：py -3 tools\\verify_sap.py

自带后端进程 + 临时数据库，不污染真实 data/onlyone.db。
会真实打开一次 SAP GUI 会话窗口（按 DS4|100），结束后关闭本次新开的窗口。
依赖：本机已安装 SAP GUI 且 SAPUILandscape.xml 中存在 DS4 客户端 100 的连接项。
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
    """只关本次回归新开的 SAP GUI 会话窗口，不碰用户原有窗口。"""
    closed = 0
    for win in sap_mod.sap_windows("sapgui.exe"):
        if win["hwnd"] not in before:
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

        before = {w["hwnd"] for w in sap_mod.sap_windows("sapgui.exe")}
        if before:
            print("NOTE 已有 %d 个 SAP GUI 会话窗口，B2 可能判定为「复用」" % len(before),
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
            results.append(check("A：DS4|100 映射到连接项 DS4-100ZH",
                                 bool(item and item["launch_name"] == "DS4-100ZH"),
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
            results.append(check("A：按绑定启动使用连接项名",
                                 launched.get("launch_name") == "DS4-100ZH",
                                 "source=%s" % launched.get("source")))
            ok = launched.get("verified") is True
            if not ok and "复用" in str(launched.get("detail", "")):
                print("NOTE 复用了已有会话窗口，verified=false 属预期降级", flush=True)
                ok = True
            results.append(check("B2：确认出现 SAP GUI 会话窗口", ok,
                                 "verified=%s front=%s" % (launched.get("verified"),
                                                           launched.get("front"))))
            results.append(check("B1：会话窗口已尝试置前",
                                 "front" in launched, "front=%s" % launched.get("front")))

            _, logon = http(base, "/api/open-sap", "POST")
            results.append(check("B1：SAP Logon 复用/启动且已置前",
                                 logon.get("verified") is True,
                                 "reused=%s front=%s" % (logon.get("reused"),
                                                         logon.get("front"))))

            # --- B2 失败路径：模拟 sapshcut 退出 0 但无窗口 ---
            class FakeProc:
                def poll(self):
                    return 0

            with patch.object(sap_mod.subprocess, "Popen", return_value=FakeProc()), \
                 patch.object(sap_mod, "_wait_new_session", return_value=None), \
                 patch.object(sap_mod, "sap_windows", return_value=[]):
                try:
                    sap_mod.launch_connection("ZZZ|999", "")
                    failed_loudly = False
                except HTTPException as exc:
                    failed_loudly = exc.status_code == 502
            results.append(check("B2：静默失败返回 502 明确错误而非假成功",
                                 failed_loudly))
            results.append(check("A：旧凭据 DS4|120 仍映射到 DS4-120ZH",
                                 sap_mod.resolve_launch_name("DS4|120")[0] == "DS4-120ZH"))

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
