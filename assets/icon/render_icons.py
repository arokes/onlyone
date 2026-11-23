# -*- coding: utf-8 -*-
"""从矢量主稿生成多尺寸 PNG + 多帧 ICO + favicon。

用法：python assets/icon/render_icons.py
依赖：Chrome（无头渲染，保证与浏览器同源抗锯齿）、Pillow（校验）。

尺寸策略（见 docs/图标设计方案.md）：
  48–256  完整版 onlyone.svg
  32–40   中尺寸特调 onlyone-mid.svg
  16–24   小尺寸特调 onlyone-small.svg
"""
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
FRONTEND_PUBLIC = PROJECT / "frontend" / "public"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PLAN = [
    (256, "onlyone.svg"),
    (128, "onlyone.svg"),
    (64, "onlyone.svg"),
    (48, "onlyone.svg"),
    (40, "onlyone-mid.svg"),
    (32, "onlyone-mid.svg"),
    (24, "onlyone-small.svg"),
    (20, "onlyone-small.svg"),
    (16, "onlyone-small.svg"),
]

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def render_png(svg_name: str, size: int, out: Path) -> None:
    svg = (ROOT / svg_name).read_text(encoding="utf-8")
    svg = svg.replace('width="1024" height="1024"', f'width="{size}" height="{size}"', 1)
    html = (
        "<!doctype html><meta charset=utf-8>"
        '<body style="margin:0;background:transparent">' + svg + "</body>"
    )
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "r.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--default-background-color=00000000",
            f"--window-size={size},{size}", "--virtual-time-budget=1500",
            f"--screenshot={out}", page.as_uri(),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_ico(frames: list[tuple[int, Path]], out: Path) -> None:
    """手工拼 ICO 容器：每帧直接内嵌对应尺寸的 PNG，避免单图缩放糊掉小尺寸。"""
    blobs = [(size, path.read_bytes()) for size, path in frames]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, data = b"", b""
    for size, blob in blobs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    out.write_bytes(header + entries + data)


def main() -> int:
    if not Path(CHROME).exists():
        raise SystemExit(f"找不到 Chrome：{CHROME}")
    tmp = ROOT / "_frames"
    tmp.mkdir(exist_ok=True)

    made: dict[int, Path] = {}
    for size, svg_name in PLAN:
        out = tmp / f"onlyone-{size}.png"
        render_png(svg_name, size, out)
        made[size] = out
        print(f"  {size:>3}px  <- {svg_name}")

    for size in (1024, 256):
        src = ROOT / "onlyone.svg"
        svg = src.read_text(encoding="utf-8").replace(
            'width="1024" height="1024"', f'width="{size}" height="{size}"', 1)
        html = "<!doctype html><body style='margin:0;background:transparent'>" + svg + "</body>"
        with tempfile.TemporaryDirectory() as td:
            page = Path(td) / "r.html"
            page.write_text(html, encoding="utf-8")
            subprocess.run([
                CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=1", "--default-background-color=00000000",
                f"--window-size={size},{size}", "--virtual-time-budget=2000",
                f"--screenshot={ROOT / f'onlyone-{size}.png'}", page.as_uri(),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    build_ico([(s, made[s]) for s in ICO_SIZES], ROOT / "onlyone.ico")
    build_ico([(16, made[16]), (32, made[32]), (48, made[48])], ROOT / "favicon.ico")

    FRONTEND_PUBLIC.mkdir(parents=True, exist_ok=True)
    (FRONTEND_PUBLIC / "favicon.ico").write_bytes((ROOT / "favicon.ico").read_bytes())
    (FRONTEND_PUBLIC / "favicon-32.png").write_bytes(made[32].read_bytes())

    from PIL import Image
    for name in ("onlyone.ico", "favicon.ico"):
        im = Image.open(ROOT / name)
        print(f"  {name}: {im.info.get('sizes')}")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
