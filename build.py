# -*- coding: utf-8 -*-
"""M5-4 打包：npm run build → PyInstaller onedir → 复制前端产物到 EXE 同级。

用法：py -3 build.py [--skip-frontend] [--skip-pyinstaller]
交付是**文件夹** ``dist/OnlyOne/``，不是单 EXE；把整个文件夹拷走即可离线运行。
运行时数据目录 ``data/`` 与前端 ``frontend/dist/`` 都在 OnlyOne.exe 同级，
由 backend/run.py 的 ONLYONE_BASE_DIR 与 db.py 的 _base_dir() 定位。
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = ROOT / "dist"
APP_DIR = DIST / "OnlyOne"
ENTRY = ROOT / "backend" / "run.py"


def run(cmd: list[str], *, cwd: Path | None = None, shell: bool = False) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True, shell=shell)


def main() -> int:
    ap = argparse.ArgumentParser(description="OnlyOne 打包")
    ap.add_argument("--skip-frontend", action="store_true", help="跳过 npm run build")
    ap.add_argument("--skip-pyinstaller", action="store_true", help="只重新复制前端产物")
    args = ap.parse_args()

    if not args.skip_frontend:
        # Windows 下 npm 是 .cmd，需要 shell=True 才能直接调用
        run(["npm", "run", "build"], cwd=FRONTEND, shell=True)

    if not args.skip_pyinstaller:
        if importlib.util.find_spec("PyInstaller") is None:
            raise SystemExit("缺少 PyInstaller：py -3 -m pip install pyinstaller")
        if APP_DIR.exists():
            shutil.rmtree(APP_DIR)
        run([
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onedir", "--name", "OnlyOne",
            "--distpath", str(DIST),
            "--workpath", str(ROOT / "build" / "pyi"),
            "--specpath", str(ROOT / "build"),
            "--hidden-import", "win32com.client",
            "--hidden-import", "pythoncom",
            "--hidden-import", "pywintypes",
            "--hidden-import", "uvicorn.logging",
            # SAP 连接设置里的原生文件夹选择框（Tcl/Tk 需随包带上）
            "--hidden-import", "tkinter",
            "--hidden-import", "tkinter.filedialog",
            str(ENTRY),
        ], cwd=ROOT)

    exe = APP_DIR / "OnlyOne.exe"
    if not exe.exists():
        raise SystemExit("未找到打包产物：%s" % exe)

    # 前端静态资源不进 bundle，直接放 EXE 同级，db.py 的 _base_dir() 会找到它
    out_frontend = APP_DIR / "frontend" / "dist"
    if out_frontend.exists():
        shutil.rmtree(out_frontend)
    out_frontend.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FRONTEND / "dist", out_frontend)
    (APP_DIR / "data").mkdir(parents=True, exist_ok=True)

    size = sum(p.stat().st_size for p in APP_DIR.rglob("*") if p.is_file())
    print("OK  EXE      : %s" % exe)
    print("OK  frontend : %s" % out_frontend)
    print("OK  data dir : %s" % (APP_DIR / "data"))
    print("OK  total    : %.1f MB（文件夹交付，不是单 EXE）" % (size / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
