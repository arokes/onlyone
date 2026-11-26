# -*- coding: utf-8 -*-
"""TOTP（RFC 6238）本地生成，2026-11-25 功能 5。

为什么自己实现而不加依赖：算法只有十几行（HMAC-SHA1 + 动态截断），
标准库 `hmac` / `base64` / `struct` 就够；引入 pyotp 只会让打包体积和供应链多一个变量。

密钥存储：与密码同样走 DPAPI（`credentials.totp_enc`），不参与全文索引，
导出时进 AES-GCM 密文。**密钥永远不出现在列表接口里**，列表只返回算好的 6 位码。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time

PERIOD = 30
DIGITS = 6


def normalize_secret(raw: str) -> str:
    """Base32 密钥容错：去掉空格 / 短横线 / 小写，保留合法字符与已有填充。"""
    return re.sub(r"[^A-Z2-7=]", "", (raw or "").upper())


def _key(secret: str) -> bytes:
    value = normalize_secret(secret)
    if not value.strip("="):
        raise ValueError("TOTP 密钥为空")
    padding = (-len(value)) % 8
    return base64.b32decode(value + "=" * padding, casefold=True)


def code_at(secret: str, at: float | None = None, period: int = PERIOD,
            digits: int = DIGITS) -> str:
    """计算某一时刻的动态码（默认当前时间）。"""
    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10 ** digits)).zfill(digits)


def remaining(at: float | None = None, period: int = PERIOD) -> int:
    """当前码还剩多少秒过期（前端画倒计时用）。"""
    moment = at if at is not None else time.time()
    return int(period - (moment % period)) or period


def is_valid_secret(raw: str) -> bool:
    """保存前校验：能被 Base32 解码即可，避免把一段乱码存进去。"""
    try:
        _key(raw)
        return True
    except Exception:  # noqa: BLE001 - 任何解码失败都视为非法
        return False
