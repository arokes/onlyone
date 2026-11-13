# -*- coding: utf-8 -*-
"""扫描件 OCR（M6-1）：单机离线、零云端依赖的模板匹配引擎与可插拔接口。

环境取证（2026-11-06）：本机 Python 3.14 没有 onnxruntime / torch / tesseract 可用，
Windows 内置 WinRT OCR 的异步桥接又被运行环境的安全策略拦截。因此默认引擎是
**本机中文字体模板匹配**：用 Pillow 把 GB2312 一级汉字 + ASCII 渲染成字形模板，
再用 numpy 对分割后的字符做汉明距离匹配。适用于清晰的印刷体截图 / 扫描件。

能力边界（必须知悉）：这不是通用神经 OCR，对艺术字体、手写、强噪声、倾斜、低分辨率
效果有限。``OcrEngine`` 接口已预留，检测到 tesseract 等专业引擎时会自动优先使用，
无需改动上层调用。
"""
from __future__ import annotations

import io
import logging
import shutil
import string
import subprocess
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .db import DATA_DIR

log = logging.getLogger("onlyone")

FONT_PATHS = (
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
)
CANVAS = 32
CELL = 24
FONT_SIZE = 24
OFFSET = (CANVAS - CELL) // 2
MAX_HAMMING = 430          # 1024 维二值向量的最大可接受汉明距离
CACHE_FILE = "ocr_templates.npz"

_lock = threading.Lock()
_templates: np.ndarray | None = None
_chars: list[str] | None = None


def _font_paths() -> list[str]:
    return [p for p in FONT_PATHS if Path(p).exists()]


def _charset() -> list[str]:
    chars: list[str] = []
    seen: set[str] = set()

    def add(c: str) -> None:
        if c and c not in seen and c.isprintable() and not c.isspace():
            seen.add(c)
            chars.append(c)

    for c in string.ascii_letters + string.digits + ".,;:!?()[]{}<>/\\-_=+*&%$#@~^|'\"":
        add(c)
    for c in "，。、；：？！（）【】《》「」『』—…·￥％":
        add(c)
    # GB2312 一级汉字（B0A1–F7FE）
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return chars


def _render_char(ch: str, font: ImageFont.FreeTypeFont) -> np.ndarray:
    """渲染字形，并用与 _match 完全相同的裁剪/缩放流程归一化，保证几何可比。"""
    img = Image.new("L", (CANVAS, CANVAS), 255)
    ImageDraw.Draw(img).text((2, 1), ch, fill=0, font=font)
    ink = np.asarray(img, dtype=np.uint8) < 160
    ys, xs = np.where(ink)
    if len(xs) == 0:
        return np.zeros((CANVAS * CANVAS,), dtype=np.uint8)
    crop = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    gray = np.where(crop, 0, 255).astype(np.uint8)
    small = Image.fromarray(gray, mode="L").resize((CELL, CELL), Image.Resampling.BILINEAR)
    bits = (np.asarray(small, dtype=np.uint8) < 160).astype(np.uint8)
    vec = np.zeros((CANVAS, CANVAS), dtype=np.uint8)
    vec[OFFSET:OFFSET + CELL, OFFSET:OFFSET + CELL] = bits
    return vec.ravel()


def _ensure_templates() -> None:
    """首次调用渲染模板并落盘缓存（data/ocr_templates.npz），后续直接加载。"""
    global _templates, _chars  # noqa: PLW0603
    if _templates is not None:
        return
    with _lock:
        if _templates is not None:
            return
        cache = DATA_DIR / CACHE_FILE
        if cache.exists():
            try:
                with np.load(str(cache), allow_pickle=False) as data:
                    _templates = data["templates"]
                    _chars = [str(x) for x in data["chars"].tolist()]
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("OCR 模板缓存损坏，将重建：%s", exc)
        fonts = _font_paths()
        if not fonts:
            raise RuntimeError("本机未找到可用的中文字体，OCR 不可用")
        chars = _charset()
        rows: list[np.ndarray] = []
        labels: list[str] = []
        for font_path in fonts:
            try:
                font = ImageFont.truetype(font_path, FONT_SIZE)
            except Exception:  # noqa: BLE001
                continue
            for ch in chars:
                rows.append(_render_char(ch, font))
                labels.append(ch)
        if not rows:
            raise RuntimeError("字形模板渲染失败")
        _templates = np.stack(rows).astype(np.uint8)
        _chars = labels
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(str(cache), templates=_templates,
                                chars=np.array(labels, dtype="U1"))
            log.info("OCR 模板已缓存：%d 字形 / %d 字体 -> %s",
                     len(chars), len(fonts), cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 模板缓存写入失败（不影响本次识别）：%s", exc)


def _segments(counts: np.ndarray) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, value in enumerate(counts):
        if value > 0 and start is None:
            start = i
        elif value == 0 and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(counts) - 1))
    return out


def _char_blocks(segments: list[tuple[int, int]], height: int) -> list[tuple[int, int]]:
    """把列投影段聚合成字符块。

    汉字偏旁之间可能出现全白列（如「恢」的「忄」与「灰」），纯列投影会把一个字
    切成窄段 + 宽段。按字高聚合：不足 0.55 字高的块与下一段合并；过宽块再等宽切分。
    """
    merged: list[tuple[int, int]] = []
    pending: list[int] | None = None
    for c0, c1 in segments:
        if pending is None:
            pending = [c0, c1]
            continue
        current_width = pending[1] - pending[0] + 1
        merged_width = c1 - pending[0] + 1
        if current_width < height * 0.55 and merged_width <= height * 1.35:
            pending[1] = c1
        else:
            merged.append((pending[0], pending[1]))
            pending = [c0, c1]
    if pending is not None:
        merged.append((pending[0], pending[1]))

    out: list[tuple[int, int]] = []
    for c0, c1 in merged:
        width = c1 - c0 + 1
        if width > height * 1.6:
            count = max(1, int(round(width / max(height, 1))))
            for i in range(count):
                x0 = c0 + i * width // count
                x1 = c0 + (i + 1) * width // count if i < count - 1 else c1 + 1
                out.append((x0, x1 - 1))
        else:
            out.append((c0, c1))
    return out


def _match(cell: np.ndarray) -> str:
    assert _templates is not None and _chars is not None
    ys, xs = np.where(cell)
    if len(xs) == 0:
        return ""
    crop = cell[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    gray = np.where(crop, 0, 255).astype(np.uint8)
    small = Image.fromarray(gray, mode="L").resize((CELL, CELL), Image.Resampling.BILINEAR)
    bits = (np.asarray(small, dtype=np.uint8) < 160).astype(np.uint8)
    vec = np.zeros((CANVAS, CANVAS), dtype=np.uint8)
    vec[OFFSET:OFFSET + CELL, OFFSET:OFFSET + CELL] = bits
    v = vec.ravel()
    distances = np.bitwise_xor(_templates, v).sum(axis=1)
    best = int(np.argmin(distances))
    if distances[best] > MAX_HAMMING:
        return "?"
    return _chars[best]


def recognize_bytes(data: bytes) -> str:
    """识别一张图片的字节流，返回按行拼接的文本。"""
    _ensure_templates()
    img = Image.open(io.BytesIO(data)).convert("L")
    ink = np.asarray(img, dtype=np.uint8) < 160
    lines: list[str] = []
    for r0, r1 in _segments(ink.sum(axis=1)):
        height = r1 - r0 + 1
        if height < 10:
            continue
        band = ink[r0:r1 + 1, :]
        line = ""
        for c0, c1 in _char_blocks(_segments(band.sum(axis=0)), height):
            if c1 - c0 >= 1:
                line += _match(band[:, c0:c1 + 1])
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def recognize(path: str | Path) -> str:
    return recognize_bytes(Path(path).read_bytes())


def recognize_data(data: bytes) -> str:
    """按优先级选用可用引擎识别图片字节流（扫描 PDF 内嵌页图走这里）。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("无可用 OCR 引擎")
    return engine.recognize_bytes(data)


class OcrEngine:
    name = "base"

    def available(self) -> bool:
        return False

    def recognize(self, path: str | Path) -> str:
        raise NotImplementedError

    def recognize_bytes(self, data: bytes) -> str:
        """默认实现：落临时文件后交给 recognize；子类可覆盖以避免磁盘 IO。"""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(data)
            tmp = handle.name
        try:
            return self.recognize(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class TemplateOcrEngine(OcrEngine):
    """默认引擎：本机字体模板匹配，完全离线。"""

    name = "windows-font-template"

    def available(self) -> bool:
        return bool(_font_paths())

    def recognize(self, path: str | Path) -> str:
        return recognize(path)

    def recognize_bytes(self, data: bytes) -> str:
        return globals()["recognize_bytes"](data)


class TesseractOcrEngine(OcrEngine):
    """可选引擎：检测到 tesseract CLI 时自动优先（需系统语言包）。"""

    name = "tesseract"

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def recognize(self, path: str | Path) -> str:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "chi_sim+eng"],
            capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
        )
        return proc.stdout.strip()


ENGINES: tuple[OcrEngine, ...] = (TesseractOcrEngine(), TemplateOcrEngine())


def get_engine() -> OcrEngine | None:
    return next((e for e in ENGINES if e.available()), None)


def status() -> dict:
    engine = get_engine()
    return {
        "available": engine is not None,
        "engine": engine.name if engine else "",
        "fonts": _font_paths(),
        "tesseract": shutil.which("tesseract"),
        "templates_cached": (DATA_DIR / CACHE_FILE).exists(),
        "mode": "离线模板匹配（清晰印刷体）" if engine and engine.name == TEMPLATE_NAME
                else ("tesseract" if engine else "不可用"),
    }


TEMPLATE_NAME = "windows-font-template"
