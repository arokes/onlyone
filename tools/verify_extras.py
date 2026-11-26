# -*- coding: utf-8 -*-
"""功能 5（TOTP）后端断言（2026-11-25）。

用法：py -3 tools\\verify_extras.py

覆盖
----
* RFC 6238 测试向量（离线，不依赖服务）；
* 凭据创建时写入密钥 → 列表返回算好的 6 位码与剩余秒数，密钥不出接口；
* 清空密钥 = 解绑（has_totp 变 false）；
* 非法 Base32 被拒绝；
* 凭据加密导出包含 totp 字段（换机恢复不会丢）。

脚本自起后端 + 独立临时目录，不读写 data\\onlyone.db。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from onlyone import totp  # noqa: E402

TOKEN = "extras-verify-20261125"
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # RFC 向量的 Base32
results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("%s %s%s" % ("PASS " if ok else "FAIL ", label, ("  " + detail) if detail else "")),
          flush=True)
    results.append(bool(ok))


def http(base: str, path: str, method: str = "GET", body=None, expect_error: bool = False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if expect_error:
            return {"__status": exc.code}
        raise


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    # ---------------------------------------------------- 离线：算法正确性
    vectors = [(59, "287082"), (1111111109, "081804"),
               (1234567890, "005924"), (2000000000, "279037")]
    ok_vectors = all(totp.code_at(SECRET, at=t) == expect for t, expect in vectors)
    check("RFC 6238 测试向量 4/4", ok_vectors)
    check("剩余秒数在 1..30", 1 <= totp.remaining() <= 30, str(totp.remaining()))
    check("密钥容错（空格/短横线/小写）",
          totp.normalize_secret("gezd gnbv-gy3t qojq") == "GEZDGNBVGY3TQOJQ")

    data_dir = Path(tempfile.mkdtemp(prefix="onlyone_extras_"))
    port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    env["ONLYONE_DATA_DIR"] = str(data_dir)
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(200):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")

        # ---------------------------------------------------- 写入与展示
        created = http(base, "/api/credentials", "POST", {
            "title": "SAP DS4 生产", "username": "10000265", "password": "p@ss",
            "totp": SECRET,
        })
        cid = created["id"]

        rows = http(base, "/api/credentials")
        row = next((r for r in rows if r["id"] == cid), None)
        check("列表返回 has_totp", row is not None and row.get("has_totp") is True,
              str(row.get("has_totp") if row else "缺失"))
        code = str(row.get("totp_code") or "") if row else ""
        check("动态码是 6 位数字", len(code) == 6 and code.isdigit(), code)

        # 允许跨 30 秒窗口：与当前时刻及其前后两个窗口任一相等即可
        expected = {totp.code_at(SECRET, at=time.time() + offset) for offset in (-30, 0, 30)}
        check("动态码与算法结果一致", code in expected,
              "got=%s expect=%s" % (code, ",".join(sorted(expected))))
        check("返回剩余秒数", 1 <= int(row.get("totp_remaining") or 0) <= 30,
              str(row.get("totp_remaining")))
        check("密钥本身不出接口", SECRET not in json.dumps(rows, ensure_ascii=False))

        # ---------------------------------------------------- 解绑与校验
        http(base, "/api/credentials/%d" % cid, "PUT", {"totp": ""})
        row2 = next(r for r in http(base, "/api/credentials") if r["id"] == cid)
        check("清空密钥 = 解绑", row2.get("has_totp") is False and row2.get("totp_code") == "",
              json.dumps({k: row2.get(k) for k in ("has_totp", "totp_code")}, ensure_ascii=False))

        bad = http(base, "/api/credentials/%d" % cid, "PUT", {"totp": "不是密钥!!"},
                   expect_error=True)
        check("非法 Base32 被拒绝", bad.get("__status") == 400, str(bad))

        # ---------------------------------------------------- 导出覆盖 TOTP
        http(base, "/api/credentials/%d" % cid, "PUT", {"totp": SECRET})
        payload = http(base, "/api/credentials/export", "POST",
                       {"passphrase": "verify-passphrase-2026"})
        check("导出载荷结构完整",
              bool(payload.get("ciphertext")) and payload.get("count") == 1,
              str(payload.get("count")))

        plain = _decrypt_export(payload, "verify-passphrase-2026")
        check("导出包含 totp 字段",
              isinstance(plain, list) and plain and "totp" in plain[0]
              and plain[0]["totp"] == SECRET,
              json.dumps({k: ("***" if k == "totp" else v) for k, v in (plain[0] or {}).items()},
                         ensure_ascii=False) if plain else "空")

        # ---------------------------------------------------- 4 双链
        nb = http(base, "/api/notebooks", "POST", {"name": "验证笔记"})
        sec = http(base, "/api/sections", "POST", {"notebook_id": nb["id"], "name": "MRP"})
        a = http(base, "/api/notes", "POST", {"section_id": sec["id"], "title": "MRP 边际码排查"})
        b = http(base, "/api/notes", "POST", {"section_id": sec["id"], "title": "月度结账清单"})

        doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "先看 [[MRP 边际码排查]]，再看 [[还没写的页面]]"}]}]}
        http(base, "/api/notes/%d" % b["id"], "PUT",
             {"content_json": json.dumps(doc, ensure_ascii=False), "revision": 0})

        a_links = http(base, "/api/notes/%d/links" % a["id"])
        check("反链命中引用页", len(a_links["backlinks"]) == 1
              and a_links["backlinks"][0]["note_id"] == b["id"],
              json.dumps(a_links["backlinks"], ensure_ascii=False))
        check("反链带来源片段",
              "[[" in (a_links["backlinks"][0]["snippet"] if a_links["backlinks"] else ""))

        b_links = http(base, "/api/notes/%d/links" % b["id"])
        targets = {o["title"]: o["note_id"] for o in b_links["outlinks"]}
        check("出链解析两条", len(b_links["outlinks"]) == 2, str(len(b_links["outlinks"])))
        check("出链能定位到已存在页面", targets.get("MRP 边际码排查") == a["id"], str(targets))
        check("不存在的页面标为待建", targets.get("还没写的页面") is None, str(targets))

        look = http(base, "/api/note-lookup?title=" + urllib.parse.quote("MRP 边际码排查"))
        check("按标题查到页面 id", look["note_id"] == a["id"], str(look))
        miss = http(base, "/api/note-lookup?title=" + urllib.parse.quote("没有这个页面"))
        check("按标题查不到返回 None", miss["note_id"] is None, str(miss))

        http(base, "/api/notes/%d" % b["id"], "PUT", {"is_private": True, "revision": 1})
        a_after = http(base, "/api/notes/%d/links" % a["id"])
        check("私密页不出现在反链里", len(a_after["backlinks"]) == 0,
              str(len(a_after["backlinks"])))

        # ---------------------------------------------------- 6 快速捕获
        hs = http(base, "/api/hotkey/status")
        check("热键状态接口可用（侧栏提示用）",
              "enabled" in hs and "hotkey" in hs, json.dumps(hs, ensure_ascii=False))

        pend = http(base, "/api/capture/pending")
        check("无热键触发时 pending=false", pend["open"] is False, str(pend))

        empty = http(base, "/api/capture", "POST", {"kind": "incident", "text": "  "},
                     expect_error=True)
        check("空内容被拒绝", empty.get("__status") == 400, str(empty))

        cap = http(base, "/api/capture", "POST",
                   {"kind": "incident", "text": "SAP-PP 计划订单重复生成，边际码改回 3"})
        check("捕获写为运维记录", cap.get("kind") == "incident" and cap.get("id"),
              str(cap))
        listed = http(base, "/api/biz/incidents")
        hit = next((r for r in listed if r["id"] == cap.get("id")), None)
        check("捕获的记录可在运维页看到",
              hit is not None and "边际码改回 3" in str(hit.get("symptom"))
              and hit.get("category") == "新需求",
              json.dumps({k: hit.get(k) for k in ("category", "status")}, ensure_ascii=False)
              if hit else "缺失")

        # 落点：默认取第一个笔记本的第一个分区
        default_target = http(base, "/api/capture/target")
        check("默认落点来自第一个分区",
              default_target["section_id"] == sec["id"],
              json.dumps({k: default_target[k] for k in ("section_id", "notebook_name", "section_name")},
                         ensure_ascii=False))
        check("落点接口带分区树",
              any(b["id"] == nb["id"] for b in default_target["notebooks"]))

        # 指定另一个分区并保存为默认
        quick = http(base, "/api/sections", "POST", {"notebook_id": nb["id"], "name": "速记"})
        saved = http(base, "/api/capture/target", "PUT", {"section_id": quick["id"]})
        check("可指定速记分区", saved["section_name"] == "速记", str(saved))
        check("选择后成为默认", http(base, "/api/capture/target")["section_id"] == quick["id"])

        note = http(base, "/api/capture", "POST",
                    {"kind": "note", "text": "速记：MRP 齐套检查明天要看"})
        check("捕获写为笔记", note.get("kind") == "note" and note.get("id"), str(note))
        # 列表接口是精简字段，单条详情才有 section_id
        detail = http(base, "/api/notes/%d" % note["id"])
        check("笔记落到指定分区", detail.get("section_id") == quick["id"],
              str(detail.get("section_id")))

        # 富文本 + 内联截图：保存时才落盘，src 被改写成附件地址
        png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
               "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "截图如下"}]},
            {"type": "image", "attrs": {"src": "data:image/png;base64," + png}},
        ]}
        rich = http(base, "/api/capture", "POST", {
            "kind": "note", "text": "截图", "title": "截图",
            "content_json": json.dumps(doc, ensure_ascii=False),
            "section_id": quick["id"],
        })
        check("富文本捕获报告落盘图片数", rich.get("images") == 1, str(rich))

        rich_detail = http(base, "/api/notes/%d" % rich["id"])
        saved_doc = json.loads(rich_detail["content_json"])
        img_src = saved_doc["content"][1]["attrs"]["src"]
        check("内联图 src 已改写为附件地址",
              img_src.startswith("/api/attachments/%d/" % rich["id"]), img_src)
        with urllib.request.urlopen(base + img_src, timeout=10) as resp:
            check("附件可访问", resp.status == 200 and len(resp.read()) > 0)

        bad_target = http(base, "/api/capture/target", "PUT", {"section_id": 999999},
                          expect_error=True)
        check("不存在的分区被拒绝", bad_target.get("__status") == 404, str(bad_target))

        dismiss = http(base, "/api/capture/dismiss", "POST", {"fore": 0})
        check("dismiss 可调用", dismiss.get("ok") is True, str(dismiss))

        # 开发开关未开时，模拟触发的接口必须 404
        trig = http(base, "/api/capture/_trigger", "POST", {}, expect_error=True)
        check("生产环境不暴露热键模拟接口", trig.get("__status") == 404, str(trig))

        # ---------------------------------------------------- 单实例检测
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("onlyone_run", BACKEND / "run.py")
            run_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(run_mod)
            # 用测试专用名字：生产 mutex 可能被用户正在运行的 OnlyOne 持有
            first = run_mod._acquire_single_instance("OnlyOne-SingleInstance-Test")
            second = run_mod._acquire_single_instance("OnlyOne-SingleInstance-Test")
            check("单实例：首个实例通过、第二个被拦住", first is True and second is False,
                  "first=%s second=%s" % (first, second))
        except Exception as exc:  # noqa: BLE001
            check("单实例检测可用", False, str(exc))

        ok = all(results)
        print("\n%s  (%d/%d)" % ("ALL PASS" if ok else "SOME FAILED",
                                 sum(results), len(results)))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)


def _decrypt_export(payload: dict, passphrase: str):
    """按 backup.py 的 Scrypt + AES-GCM 参数解开导出载荷（只为断言字段）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt = base64.b64decode(payload["salt"])
    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    key = Scrypt(salt=salt, length=32, n=payload["n"], r=payload["r"], p=payload["p"]).derive(
        passphrase.encode("utf-8"))
    return json.loads(AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
