# -*- coding: utf-8 -*-
"""业务记录的图片通道：内容寻址的附件池（记事本之外的第二类附件）。

设计要点
--------
1. **与记录 id 解耦**。新增弹窗是「先填后存」，记录 id 在保存后才存在，
   图片若挂 ``attachments/<id>/`` 就只能先建空记录或保存后搬文件。这里按
   内容哈希落盘到 ``data/attachments/blob/<sha256 前 32 位>.<ext>``，
   草稿态即可上传，保存后无需重写 URL。
2. **天然去重**。文件名即内容哈希，同一张截图贴进多条记录只占一份磁盘。
3. **不做垃圾回收**。删记录时不删 blob（可能被其他记录引用）。孤立图片
   需要清理时另行实现扫描，不在写入路径上做代价不可控的事。
4. **鉴权沿用全局令牌**：``/api/attachments/`` 前缀已在中间件放行，
   因为 ``<img src>`` 无法携带自定义头；本地随机端口 + 一次性令牌保护其余 API。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .db import DATA_DIR

log = logging.getLogger("onlyone")
router = APIRouter()

BLOB_DIR = DATA_DIR / "attachments" / "blob"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SAFE_BLOB = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
# 与 notes.SAFE_FILENAME 同源：只认这四种魔数通过检验的位图
_EXT_MAGIC = (
    ("png", lambda raw: raw.startswith(b"\x89PNG\r\n\x1a\n")),
    ("jpg", lambda raw: raw.startswith(b"\xff\xd8\xff")),
    ("gif", lambda raw: raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a")),
    ("webp", lambda raw: raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"),
)


def image_ext(raw: bytes) -> str | None:
    """按魔数判定位图格式；不支持的格式返回 ``None``（记事本上传也走这里）。"""
    for ext, check in _EXT_MAGIC:
        try:
            if check(raw):
                return ext
        except Exception:  # noqa: BLE001 - 魔数判定不该抛
            continue
    return None


class ImageIn(BaseModel):
    data: str                      # data:image/...;base64,...
    name: str = "image.png"


@router.post("/api/attachments/images")
def upload_image(body: ImageIn):
    """base64 上传（不引入 python-multipart），返回可直接写进富文本的 URL。"""
    if not body.data.startswith("data:image/"):
        raise HTTPException(400, "只接受 data:image/ 开头的图片")
    try:
        _, encoded = body.data.split(",", 1)
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "图片数据不是合法 base64") from exc
    if not raw:
        raise HTTPException(400, "图片为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "图片超过 10 MB 上限")
    ext = image_ext(raw)
    if ext is None:
        raise HTTPException(400, "不支持的图片格式（仅 png/jpg/gif/webp）")

    filename = "%s.%s" % (hashlib.sha256(raw).hexdigest()[:32], ext)
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    target = BLOB_DIR / filename
    dedup = target.is_file()
    if not dedup:
        target.write_bytes(raw)
    return {"url": "/api/attachments/blob/%s" % filename, "filename": filename,
            "size": len(raw), "dedup": dedup}


@router.get("/api/attachments/blob/{filename}")
def get_blob(filename: str):
    if not SAFE_BLOB.fullmatch(filename):
        raise HTTPException(400, "非法文件名")
    path = BLOB_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(str(path), headers={"Cache-Control": "private, max-age=86400"})
