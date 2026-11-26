# -*- coding: utf-8 -*-
"""凭据导出 / 导入（M7，用户确认**只做 C 方案**）。

Scrypt 派生密钥 + AES-GCM 加密：导出文件里只有密文，可跨机器 / 跨账户恢复；
导入时用**当前 Windows 账户的 DPAPI 重新加密**，因此恢复后的凭据仍免主口令。

安全约束：

* 明文只在内存中出现，不写日志、不写响应、不落临时文件；
* 口令错误 / 文件损坏由 AES-GCM 认证失败判定，不回显任何明文；
* 不做明文 JSON/CSV 导出（与设计方案 §10「不把密码写入导出文件」冲突）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .db import connect, decrypt, encrypt

log = logging.getLogger("onlyone")
router = APIRouter()

FORMAT = "onlyone-credentials-v1"
KDF = "scrypt"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
MIN_PASSPHRASE = 8


class ExportIn(BaseModel):
    passphrase: str


class ImportIn(BaseModel):
    passphrase: str
    payload: dict
    mode: str = "merge"          # merge 追加 / replace 清空后导入


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=n, r=r, p=p, dklen=KEY_LEN, maxmem=64 * 1024 * 1024,
    )


@router.post("/api/credentials/export")
def export_credentials(body: ExportIn):
    """返回纯密文载荷；前端生成 .json 文件下载。"""
    if len(body.passphrase) < MIN_PASSPHRASE:
        raise HTTPException(400, "导出口令至少 8 个字符")
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM credentials ORDER BY id")]
    finally:
        conn.close()

    try:
        plain = [{
            "title": row["title"],
            "username": decrypt(row["username_enc"]),
            "password": decrypt(row["password_enc"]),
            "url": decrypt(row["url_enc"]),
            "note": decrypt(row["note_enc"]),
            # 动态码密钥也进密文：否则换机恢复后 TOTP 全丢（2026-11-25）
            "totp": decrypt(row["totp_enc"]) if row["totp_enc"] else "",
            "project_id": row["project_id"],
            "sap_conn": row.get("sap_conn") or "",
        } for row in rows]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "凭据解密失败：数据可能来自其他 Windows 账户") from exc

    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = AESGCM(_derive(body.passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(
        nonce, json.dumps(plain, ensure_ascii=False).encode("utf-8"), None)
    log.info("凭据加密导出完成 count=%s（仅密文）", len(plain))
    return {
        "format": FORMAT, "kdf": KDF,
        "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
        "salt": _b64(salt), "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext), "count": len(plain),
    }


@router.post("/api/credentials/import")
def import_credentials(body: ImportIn):
    """解密后用当前账户 DPAPI 重新加密写入，实现跨机恢复。"""
    payload = body.payload or {}
    if payload.get("format") != FORMAT or payload.get("kdf") != KDF:
        raise HTTPException(400, "不是受支持的凭据导出文件")
    if body.mode not in ("merge", "replace"):
        raise HTTPException(400, "mode 只能是 merge 或 replace")
    try:
        salt = _unb64(payload["salt"])
        nonce = _unb64(payload["nonce"])
        ciphertext = _unb64(payload["ciphertext"])
        key = _derive(body.passphrase, salt,
                      int(payload.get("n", SCRYPT_N)),
                      int(payload.get("r", SCRYPT_R)),
                      int(payload.get("p", SCRYPT_P)))
        items = json.loads(AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8"))
    except (InvalidTag, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise HTTPException(400, "口令错误或导出文件已损坏") from exc
    if not isinstance(items, list):
        raise HTTPException(400, "导出文件内容格式不正确")

    conn = connect()
    try:
        if body.mode == "replace":
            conn.execute("DELETE FROM credentials")
        imported = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            conn.execute(
                """INSERT INTO credentials
                   (title,username_enc,password_enc,url_enc,note_enc,totp_enc,
                    project_id,sap_conn,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                (str(item.get("title") or "未命名凭据"),
                 encrypt(str(item.get("username") or "")),
                 encrypt(str(item.get("password") or "")),
                 encrypt(str(item.get("url") or "")),
                 encrypt(str(item.get("note") or "")),
                 encrypt(str(item.get("totp") or "")) if item.get("totp") else None,
                 item.get("project_id"),
                 str(item.get("sap_conn") or "")),
            )
            imported += 1
        conn.commit()
        log.info("凭据导入完成 mode=%s count=%s（不记录明文）", body.mode, imported)
        return {"imported": imported, "mode": body.mode}
    finally:
        conn.close()
