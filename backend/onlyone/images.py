# -*- coding: utf-8 -*-
"""只读预览的图片通道：枚举文档内嵌图片，按需转码后直接回给浏览器。

设计取舍
--------
1. **只读 ``*/media/`` 部件**。Word 给每个 OLE 对象（含「选择性粘贴」成 PBrush
   的截图）都在 ``word/media/`` 存了一张预览图；实测本库 58 个 OLE 位图 100%
   能在同一文档的 media 里找到同尺寸对应图，所以不再解析 ``oleObject*.bin``
   的 ``Ole10Native``，避免同一张截图出现两次。
2. **不落盘、不入库**。转码结果只进内存 LRU；图片字节不参与全文索引，
   因此不会让 ``onlyone.db`` 变大。
3. **不可渲染的格式直接跳过**：``.wdp``(JPEG XR)、``.svg``、WPS 私有扩展名
   （``.534c5550`` 之类）Pillow 与浏览器都不可靠，宁可少列也不要出一堆破图。
"""
from __future__ import annotations

import io
import logging
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

MEDIA_PREFIXES = ("word/media/", "ppt/media/", "xl/media/")
SUPPORTED_EXTS = {".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm"}

# 浏览器能直接显示的位图；EMF/WMF 需要先用 Pillow（Windows GDI）栅格化
_NATIVE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".gif": "image/gif", ".bmp": "image/bmp"}
_VECTOR = {".emf", ".wmf"}

# 小图标（项目符号、勾选框、装饰线）不展示——库里 4387 张图大多是这个量级
MIN_SIDE = 64
MIN_BYTES = 3 * 1024
MAX_LIST = 300

THUMB_MAX = 320
PREVIEW_MAX = 1600
# 小于该体积、且不需要缩略图时原样返回，省掉一次重编码
RAW_PASSTHROUGH = 1_500_000

_LIST_CACHE_MAX = 8
_LIST_CACHE: "OrderedDict[tuple, list[ImageInfo]]" = OrderedDict()
_BYTE_CACHE: "OrderedDict[tuple, tuple[bytes, str]]" = OrderedDict()
_BYTE_CACHE_BUDGET = 48 * 1024 * 1024
_BYTE_CACHE_SIZE = [0]
_LOCK = threading.Lock()

_NUMS = re.compile(r"(\d+)")


@dataclass
class ImageInfo:
    """一张可预览的图片；``index`` 与预览正文里 ``[图 N]`` 的 N-1 对应。"""

    index: int
    name: str
    kind: str          # native | vector
    width: int
    height: int
    bytes: int
    # zip 内完整路径（word/media/... / ppt/media/...），不下发给前端
    part: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "part"}


def _natural_key(name: str):
    """image2 排在 image10 前面，而不是按字典序。"""
    return [int(x) if x.isdigit() else x.lower() for x in _NUMS.split(name)]


def _stat_key(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), int(st.st_mtime_ns), st.st_size)


def list_images(path: Path) -> list[ImageInfo]:
    """枚举文档内可预览的图片；不支持的文件类型返回空列表，永不抛异常。"""
    key = _stat_key(path)
    if key is None:
        return []
    with _LOCK:
        hit = _LIST_CACHE.get(key)
        if hit is not None:
            _LIST_CACHE.move_to_end(key)
            return hit
    items = _scan(path)
    with _LOCK:
        _LIST_CACHE[key] = items
        _LIST_CACHE.move_to_end(key)
        while len(_LIST_CACHE) > _LIST_CACHE_MAX:
            _LIST_CACHE.popitem(last=False)
    return items


def _scan(path: Path) -> list[ImageInfo]:
    if path.suffix.lower() not in SUPPORTED_EXTS:
        return []
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return []
    items: list[ImageInfo] = []
    with z:
        parts = [n for n in z.namelist()
                 if not n.endswith("/") and n.startswith(MEDIA_PREFIXES)]
        parts.sort(key=_natural_key)
        for part in parts:
            ext = Path(part).suffix.lower()
            if ext not in _NATIVE and ext not in _VECTOR:
                continue
            try:
                info = z.getinfo(part)
            except KeyError:
                continue
            if info.file_size < MIN_BYTES:
                continue
            try:
                if ext in _NATIVE:
                    with z.open(part) as fp, Image.open(fp) as im:
                        width, height = im.size
                else:
                    with Image.open(io.BytesIO(z.read(part))) as im:
                        width, height = im.size
            except Exception:  # noqa: BLE001 - 单张坏图不该影响整份文档
                continue
            if min(width, height) < MIN_SIDE:
                continue
            items.append(ImageInfo(
                index=len(items),
                name=part.rsplit("/", 1)[-1],
                kind="native" if ext in _NATIVE else "vector",
                width=width, height=height, bytes=info.file_size,
                part=part,
            ))
            if len(items) >= MAX_LIST:
                break
    return items


def docx_rel_map(path: Path) -> dict[str, int]:
    """``rId`` → 图片序号（0 基），供抽取器在正文里插 ``[图 N]`` 标记。

    只处理 ``word/document.xml`` 的关系表；页眉页脚与脚注的图不参与正文标记。
    """
    items = list_images(path)
    if not items:
        return {}
    by_part = {it.name: it.index for it in items}
    rels = "word/_rels/document.xml.rels"
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return {}
    mapping: dict[str, int] = {}
    with z:
        try:
            xml = z.read(rels).decode("utf-8", "ignore")
        except KeyError:
            return {}
    for rid, target in re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', xml):
        name = target.split("/")[-1]
        idx = by_part.get(name)
        if idx is not None:
            mapping[rid] = idx
    return mapping


def read_image(path: Path, index: int, *, thumb: bool = False) -> tuple[bytes, str]:
    """取第 ``index`` 张图，返回 ``(字节, MIME)``；越界或转码失败抛异常。"""
    items = list_images(path)
    if index < 0 or index >= len(items):
        raise IndexError("图片序号越界：%s" % index)
    item = items[index]
    stat = _stat_key(path) or (str(path), 0, 0)
    key = (stat[0], stat[1], stat[2], index, thumb)
    with _LOCK:
        hit = _BYTE_CACHE.get(key)
        if hit is not None:
            _BYTE_CACHE.move_to_end(key)
            return hit

    data, mime = _render(path, item, thumb)

    with _LOCK:
        _BYTE_CACHE[key] = (data, mime)
        _BYTE_CACHE.move_to_end(key)
        _BYTE_CACHE_SIZE[0] += len(data)
        while _BYTE_CACHE_SIZE[0] > _BYTE_CACHE_BUDGET and len(_BYTE_CACHE) > 1:
            _, (old, _m) = _BYTE_CACHE.popitem(last=False)
            _BYTE_CACHE_SIZE[0] -= len(old)
    return data, mime


def _render(path: Path, item: ImageInfo, thumb: bool) -> tuple[bytes, str]:
    with zipfile.ZipFile(path) as z:
        raw = z.read(item.part)
    ext = Path(item.name).suffix.lower()
    limit = THUMB_MAX if thumb else PREVIEW_MAX

    if ext in _VECTOR:
        # Pillow 在 Windows 上经 GDI 栅格化，实测矢量图文字清晰
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            img = im.convert("RGB")
        return _encode(img, limit, ".png")

    if not thumb and len(raw) <= RAW_PASSTHROUGH:
        return raw, _NATIVE[ext]

    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        img = im.copy() if im.mode in ("RGB", "RGBA", "L") else im.convert("RGB")
    return _encode(img, limit, ext)


def _encode(img: Image.Image, limit: int, ext: str) -> tuple[bytes, str]:
    if max(img.size) > limit:
        img = img.copy()
        img.thumbnail((limit, limit), Image.LANCZOS)
    buf = io.BytesIO()
    if ext in (".jpg", ".jpeg") and img.mode in ("RGB", "L"):
        img.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue(), "image/jpeg"
    if img.mode not in ("RGB", "RGBA", "L", "P"):
        img = img.convert("RGB")
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), "image/png"
