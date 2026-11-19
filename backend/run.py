# -*- coding: utf-8 -*-
"""OnlyOne 入口：本地 API + pywebview 桌面窗口（单进程）。

开发：  py -3 run.py --no-window --port 8765
生产：  py -3 run.py                    （随机空闲端口 + 一次性令牌 + 桌面窗口）
降级：  py -3 run.py --browser         （没装 WebView2 或窗口白屏时用系统浏览器）
低干扰：py -3 run.py --no-watcher      （桌面窗口，跳过启动全量校准扫描）

退出：桌面窗口关闭即停服并结束进程；--browser / --no-window 用 Ctrl+C 退出。
令牌只在本进程内有效，退出即失效；API 只监听 127.0.0.1。
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

# PyInstaller 打包后，数据目录与 frontend/dist 都在 EXE 同级，而不是 bundle 内部。
# 必须在 import onlyone.db（模块导入时计算 PROJECT_ROOT）之前设置。
if getattr(sys, "frozen", False):
    os.environ.setdefault("ONLYONE_BASE_DIR", str(Path(sys.executable).resolve().parent))

import uvicorn  # noqa: E402
from onlyone.app import TOKEN_ENV, create_app  # noqa: E402
from onlyone.db import DATA_DIR  # noqa: E402

WINDOW_TITLE = "OnlyOne - 个人工作台"


def _say(text: str) -> None:
    """窗口化（无控制台）运行时 ``sys.stdout`` 为 None，print 会抛 AttributeError。"""
    if sys.stdout is not None:
        try:
            print(text, flush=True)
        except Exception:  # noqa: BLE001 - 输出失败不得影响主流程
            pass


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(str(DATA_DIR / "onlyone.log"), maxBytes=2 * 1024 * 1024,
                             backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # 打包用 --noconsole（GUI 子系统），此时没有标准输出，挂 StreamHandler 会在
    # 每次写日志时抛 AttributeError。只有真正有控制台（开发期 / --no-window）才挂。
    if sys.stdout is not None:
        root.addHandler(logging.StreamHandler(sys.stdout))


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred if preferred > 0 else 0))
            return s.getsockname()[1]
        except OSError as exc:
            if preferred > 0:
                raise SystemExit("端口 %d 被占用：%s" % (preferred, exc)) from exc
            raise


def _wait_ready(base: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/meta/status", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.15)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="OnlyOne 个人工作台")
    ap.add_argument("--no-window", action="store_true", help="只起 API，不开窗口")
    ap.add_argument("--browser", action="store_true", help="用系统默认浏览器打开")
    ap.add_argument("--port", type=int, default=0, help="指定端口，0=随机空闲端口")
    ap.add_argument("--no-watcher", action="store_true",
                    help="禁用文件监听与启动全量校准（低干扰模式，默认行为不变）")
    args = ap.parse_args()

    setup_logging()
    log = logging.getLogger("onlyone")
    token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(24)
    os.environ[TOKEN_ENV] = token
    port = _free_port(args.port)
    app = create_app(token)

    # log_config=None：uvicorn 默认日志配置会写死 ext://sys.stdout，而 --noconsole
    # 打包后 sys.stdout 为 None，dictConfig 会抛 ValueError('Unable to configure formatter')。
    # 本进程已有文件日志（setup_logging），这里跳过 uvicorn 自己的配置即可。
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                         access_log=False, log_config=None)
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None
    worker = threading.Thread(target=server.run, daemon=True, name="onlyone-uvicorn")
    worker.start()

    base = "http://127.0.0.1:%d" % port
    if not _wait_ready(base):
        raise SystemExit("后端启动超时，请查看日志：%s" % (DATA_DIR / "onlyone.log"))
    url = base + "/?token=" + token
    log.info("OnlyOne ready at %s", base)
    _say("OnlyOne ready: " + base)

    # M5-1：生产形态启动 watchdog；回归脚本用 --no-window 默认不启用，
    # 可用 ONLYONE_WATCHER=1 显式开启（verify_m5.py 用）。
    watcher_enabled = ((not args.no_window)
                       or os.environ.get("ONLYONE_WATCHER") == "1")
    if watcher_enabled and args.no_watcher:
        # 启动全量校准会持续占用写锁与 CPU（实测单次扫描可达数分钟），
        # 期间业务 CRUD / 检索响应显著变慢。需要低干扰时用本开关跳过。
        log.info("已按 --no-watcher 跳过文件监听与启动全量校准")
    elif watcher_enabled:
        try:
            from onlyone.watcher import start_watcher

            start_watcher()
            log.info("watcher 已启用")
        except Exception as exc:  # noqa: BLE001
            # 监听不可用时不影响程序本体：手动扫描 / 重建索引仍然可用。
            log.warning("watcher 启动失败（自动增量索引不可用）：%s", exc)

    def _flush_logs() -> None:
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:  # noqa: BLE001
                pass

    def _force_exit_if_stuck() -> None:
        """关窗后仍被 pywebview / CLR 的非守护线程挂住时的兜底。"""
        log.warning("窗口关闭后 8 秒仍未退出，强制结束进程")
        _flush_logs()
        os._exit(0)

    def _shutdown() -> None:
        """所有退出路径统一停服，保证 PowerShell 能回到提示符。"""
        server.should_exit = True
        worker.join(timeout=5)
        if worker.is_alive():
            log.warning("uvicorn 线程 5 秒内未退出，进程将强制结束")
        log.info("OnlyOne 已退出")
        _flush_logs()
        os._exit(0)

    try:
        if args.no_window:
            worker.join()
        elif args.browser:
            webbrowser.open(url)
            _say("浏览器模式：关闭窗口后请按 Ctrl+C 退出")
            worker.join()
        else:
            try:
                import webview

                window = webview.create_window(
                    WINDOW_TITLE, url, width=1280, height=800, min_size=(960, 600))

                def _on_closing(*_args) -> None:
                    server.should_exit = True
                    watchdog = threading.Timer(8.0, _force_exit_if_stuck)
                    watchdog.daemon = True
                    watchdog.start()

                window.events.closing += _on_closing
                webview.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("桌面窗口不可用，改用系统浏览器：%s", exc)
                webbrowser.open(url)
                _say("浏览器模式：关闭窗口后请按 Ctrl+C 退出")
                worker.join()
    except KeyboardInterrupt:
        log.info("收到中断，正在退出")
    finally:
        _shutdown()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 无控制台时异常信息只进日志文件
        _log = logging.getLogger("onlyone")
        if not logging.getLogger().handlers:
            try:
                setup_logging()
            except Exception:  # noqa: BLE001
                pass
        _log.exception("OnlyOne 启动失败")
        raise
