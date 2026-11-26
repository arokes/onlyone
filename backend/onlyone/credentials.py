# -*- coding: utf-8 -*-
"""凭据库 API（M4）：DPAPI 逐字段加密，免主口令直接查看。

安全设计（v1.0 §2 硬约束 3 / §3，2026-11-09 按用户确认调整）：

* 列表接口**四个字段全部明文返回**（本地单人工作台，默认一眼可见，不做隐藏）。
  存储层仍是 DPAPI 加密；明文只出现在 API 响应与前端内存，不写日志、不进导出文件。
* ``credentials`` 表不登记进 ``entries.ENTITIES``，**永不写入 FTS 索引**。
* 所有日志只记凭据 id / 字段名，不记任何字段值；解密失败返回固定信息，不回显密文或异常细节。
* ``open-url`` 在服务端解密后打开，URL 不回显给前端、不写日志（URL 可能含令牌参数）。
"""
from __future__ import annotations

import logging
import webbrowser

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import totp
from .db import connect, decrypt, encrypt

log = logging.getLogger("onlyone")
router = APIRouter()

ENC_FIELDS = ("username", "password", "url", "note")


class CredentialIn(BaseModel):
    title: str | None = None
    username: str | None = None
    password: str | None = None
    url: str | None = None
    note: str | None = None
    project_id: int | None = None
    # 绑定的 SAP 连接标识（如 "QS4|100"），明文存储，非敏感
    sap_conn: str | None = None
    # 两步验证密钥（Base32）。留空表示不修改；空串表示解绑。
    totp: str | None = None


def _row(conn, cid: int):
    return conn.execute("SELECT * FROM credentials WHERE id=?", (cid,)).fetchone()


def _public(row) -> dict:
    """列表视图：四个加密字段全部解密返回明文（按使用需求默认不隐藏）。

    密文仍只在数据库里；解密失败时该字段返回空串并记一条不含值的告警，
    不回显密文或异常细节。``has_*`` 保留，供前端判断按钮是否可用。
    """
    out = {k: row[k] for k in ("id", "title", "project_id", "updated_at")}
    out["sap_conn"] = row["sap_conn"] or ""
    # 动态码：只返回算好的 6 位与剩余秒数，密钥不出接口（前端也不需要）
    out["has_totp"] = bool(row["totp_enc"])
    out["totp_code"] = ""
    out["totp_remaining"] = 0
    if row["totp_enc"]:
        try:
            secret = decrypt(row["totp_enc"])
            out["totp_code"] = totp.code_at(secret)
            out["totp_remaining"] = totp.remaining()
        except Exception:  # noqa: BLE001 - 解密失败按未绑定展示
            log.warning("凭据 id=%s TOTP 密钥不可用", row["id"])
    for field in ENC_FIELDS:
        blob = row[field + "_enc"]
        has = bool(blob)
        out["has_" + field] = has
        if not has:
            out[field] = ""
            continue
        try:
            out[field] = decrypt(blob)
        except Exception:  # noqa: BLE001 - 跨账户/跨机的旧密文按空值展示
            log.warning("凭据 id=%s 字段 %s 解密失败", row["id"], field)
            out[field] = ""
    return out


@router.get("/api/credentials")
def list_credentials():
    conn = connect()
    try:
        return [_public(r) for r in conn.execute(
            "SELECT * FROM credentials ORDER BY id DESC")]
    finally:
        conn.close()


@router.get("/api/credentials/{cid}/reveal")
def reveal_credential(cid: int, field: str):
    """按字段单次解密；field 白名单，失败不回显任何细节。"""
    if field not in ENC_FIELDS:
        raise HTTPException(400, "不支持的字段：%s" % field)
    conn = connect()
    try:
        row = _row(conn, cid)
        if row is None:
            raise HTTPException(404, "凭据不存在：%s" % cid)
        try:
            value = decrypt(row[field + "_enc"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                500, "解密失败：数据可能来自其他 Windows 账户或机器") from exc
        return {"field": field, "value": value}
    finally:
        conn.close()


@router.post("/api/credentials/{cid}/open-url")
def open_credential_url(cid: int):
    """用默认浏览器打开凭据里的链接；URL 在服务端解密，不回显、不写日志。"""
    conn = connect()
    try:
        row = _row(conn, cid)
        if row is None:
            raise HTTPException(404, "凭据不存在：%s" % cid)
        try:
            url = (decrypt(row["url_enc"]) if row["url_enc"] else "").strip()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                500, "解密失败：数据可能来自其他 Windows 账户或机器") from exc
    finally:
        conn.close()
    if not url:
        raise HTTPException(400, "该凭据未填写链接")
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "链接必须以 http:// 或 https:// 开头")
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "打开失败：%s" % exc) from exc
    return {"opened": True}


@router.post("/api/credentials")
def create_credential(body: CredentialIn):
    if not (body.title or "").strip():
        raise HTTPException(400, "标题不能为空")
    # 注意区分「没填」和「填了但非法」：normalize 会把中文/符号全部滤掉，
    # 若不先看原始输入，用户把乱码粘进来会被静默当成「未绑定」。
    totp_raw = str(body.totp or "").strip()
    totp_secret = totp.normalize_secret(totp_raw)
    if totp_raw and not totp_secret:
        raise HTTPException(400, "动态码密钥不是合法的 Base32")
    if totp_secret and not totp.is_valid_secret(totp_secret):
        raise HTTPException(400, "动态码密钥不是合法的 Base32")
    conn = connect()
    try:
        cid = conn.execute(
            """INSERT INTO credentials
               (title,username_enc,password_enc,url_enc,note_enc,totp_enc,
                project_id,sap_conn,updated_at)
               VALUES(?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (body.title.strip(),
             encrypt(body.username or ""), encrypt(body.password or ""),
             encrypt(body.url or ""), encrypt(body.note or ""),
             # 空密钥写 NULL：否则 encrypt("") 也是非空 BLOB，会被当成「已绑定」
             encrypt(totp_secret) if totp_secret else None,
             body.project_id, (body.sap_conn or "").strip()),
        ).lastrowid
        conn.commit()
        log.info("凭据新增 id=%s", cid)
        return {"id": cid}
    finally:
        conn.close()


@router.put("/api/credentials/{cid}")
def update_credential(cid: int, body: CredentialIn):
    conn = connect()
    try:
        if _row(conn, cid) is None:
            raise HTTPException(404, "凭据不存在：%s" % cid)
        data = body.model_dump(exclude_unset=True)
        sets: list[str] = []
        params: list = []
        if "title" in data:
            title = str(data["title"] or "").strip()
            if not title:
                raise HTTPException(400, "标题不能为空")
            sets.append("title=?")
            params.append(title)
        if "project_id" in data:
            sets.append("project_id=?")
            params.append(data["project_id"])
        if "sap_conn" in data:
            sets.append("sap_conn=?")
            params.append(str(data["sap_conn"] or "").strip())
        for field in ENC_FIELDS:
            if field in data:
                sets.append(field + "_enc=?")
                params.append(encrypt(data[field] or ""))
        if "totp" in data:
            original = str(data["totp"] or "").strip()
            raw = totp.normalize_secret(original)
            if original and not raw:
                raise HTTPException(400, "动态码密钥不是合法的 Base32")
            if raw and not totp.is_valid_secret(raw):
                raise HTTPException(400, "动态码密钥不是合法的 Base32")
            sets.append("totp_enc=?")
            params.append(encrypt(raw) if raw else None)
        if sets:
            sets.append("updated_at=datetime('now','localtime')")
            conn.execute("UPDATE credentials SET " + ", ".join(sets) + " WHERE id=?",
                         [*params, cid])
        conn.commit()
        log.info("凭据更新 id=%s", cid)
        return {"id": cid}
    finally:
        conn.close()


@router.delete("/api/credentials/{cid}")
def delete_credential(cid: int):
    conn = connect()
    try:
        if _row(conn, cid) is None:
            raise HTTPException(404, "凭据不存在：%s" % cid)
        conn.execute("DELETE FROM credentials WHERE id=?", (cid,))
        conn.commit()
        log.info("凭据删除 id=%s", cid)
        return {"deleted": cid}
    finally:
        conn.close()
