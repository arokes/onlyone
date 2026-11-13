# -*- coding: utf-8 -*-
"""M6 验收集：扫描件 OCR、语义相近检索、可插拔外部索引、凭据排除仍生效。

用法：py -3 tools\\verify_m6.py
插件启用测试会另起一个带 ONLYONE_KENSAKU_CLI 的后端进程（模拟外部索引 CLI）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
from onlyone.db import connect  # noqa: E402

TOKEN = "m6-verify-token-20261106"
PLUGIN_TOKEN = "m6-plugin-token-20261106"
PREFIX = "M6验证-"
FONT = r"C:\Windows\Fonts\simhei.ttf"


def http(base: str, path: str, method: str = "GET", body=None, token: str = TOKEN):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def make_image(path: Path, text: str) -> None:
    img = Image.new("RGB", (1100, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((30, 60), text, fill="black", font=ImageFont.truetype(FONT, 52))
    img.save(str(path))


def make_scanned_pdf(path: Path, text: str) -> None:
    """Pillow 直接生成单页位图 PDF，等价于扫描件（无文本层）。"""
    img = Image.new("RGB", (1100, 200), "white")
    ImageDraw.Draw(img).text((30, 60), text, fill="black",
                             font=ImageFont.truetype(FONT, 52))
    img.save(str(path), "PDF", resolution=150.0)


def purge() -> None:
    conn = connect()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM roots WHERE path LIKE ?", ("%onlyone_m6_%",))]
        for rid in ids:
            for f in conn.execute("SELECT id FROM files WHERE root_id=?", (rid,)).fetchall():
                conn.execute("DELETE FROM fts_entries WHERE entry_id IN "
                             "(SELECT id FROM entries WHERE type='file' AND ref_id=?)", (f[0],))
                conn.execute("DELETE FROM entries WHERE type='file' AND ref_id=?", (f[0],))
            conn.execute("DELETE FROM files WHERE root_id=?", (rid,))
            conn.execute("DELETE FROM roots WHERE id=?", (rid,))
        for table, col in (("projects", "name"), ("links", "title"),
                           ("worklogs", "content"), ("incidents", "system")):
            conn.execute(f"DELETE FROM {table} WHERE {col} LIKE ?", (PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


PLUGIN_CLI = """import sys, json
q = ''
if '--query' in sys.argv:
    q = sys.argv[sys.argv.index('--query') + 1]
print(json.dumps([{
    'title': '外部索引-灾备演练方案',
    'snippet': '外部结果：' + q,
    'url': 'https://example.com/doc'
}], ensure_ascii=False))
"""


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    results: list[bool] = []
    tmp: Path | None = None
    rid = None
    log_id = None
    plugin_proc = None
    try:
        if not wait_ready(base):
            raise RuntimeError("后端未就绪，检查 data/onlyone.log")
        purge()
        tmp = Path(tempfile.mkdtemp(prefix="onlyone_m6_"))

        # 1) OCR 引擎状态
        ocr_status = http(base, "/api/ocr/status")
        print("OCR STATUS", ocr_status, flush=True)
        results.append(check("OCR 引擎可用", ocr_status["available"]))

        # 2) 图片扫描件 + 扫描 PDF + 敏感图片
        make_image(tmp / "灾备扫描件.png", "灾备恢复手册数据库备份")
        make_scanned_pdf(tmp / "扫描版手册.pdf", "容灾切换流程说明")
        make_image(tmp / "系统密码清单.png", "管理员密码不应进索引")
        rid = http(base, "/api/roots", "POST",
                   {"path": str(tmp), "kind": "local"})["id"]
        scan = http(base, "/api/roots/%d/scan" % rid, "POST", {})
        print("SCAN", {k: scan[k] for k in
                       ("scanned", "extracted", "extract_failed", "sensitive", "seconds")},
              flush=True)
        results.append(check("扫描文件被处理", scan["scanned"] >= 3))

        q_ocr = "/api/search?q=" + urllib.parse.quote("灾备恢复手册")
        res = http(base, q_ocr)
        png_hits = [i for i in res["items"] if str(i["title"]).endswith(".png")]
        results.append(check("图片扫描件正文可检索", bool(png_hits),
                             str([i["title"] for i in png_hits])))

        q_pdf = "/api/search?q=" + urllib.parse.quote("容灾切换流程")
        pdf_hits = [i for i in http(base, q_pdf)["items"]
                    if str(i["title"]).endswith(".pdf")]
        results.append(check("扫描 PDF 页图 OCR 可检索", bool(pdf_hits),
                             str([i["title"] for i in pdf_hits])))

        secret = http(base, "/api/search?q=" + urllib.parse.quote("密码"))
        results.append(check("敏感图片不进 FTS",
                             not any("密码" in str(i["title"]) for i in secret["items"])))

        # 3) 语义检索：字面无命中，但语义分组命中
        log = http(base, "/api/biz/worklogs", "POST",
                   {"data": {"log_date": "2026-11-06",
                             "content": PREFIX + "服务不可用，需要立即处理", "minutes": 15}})
        log_id = log["id"]
        sem_q = "/api/search?q=" + urllib.parse.quote("宕机")
        sem = http(base, sem_q)
        literal_has = any(i["type"] == "worklog" and i["ref_id"] == log_id
                          for i in sem["items"])
        semantic_has = any(i["type"] == "worklog" and i["ref_id"] == log_id
                           for i in sem["semantic_items"])
        results.append(check("字面检索不含近义记录", not literal_has))
        results.append(check("语义分组命中近义记录", semantic_has,
                             "semantic=%d" % len(sem["semantic_items"])))

        # 4) 删除记录后语义索引同步清理
        http(base, "/api/biz/worklogs/%d" % log_id, "DELETE")
        log_id = None
        after = http(base, sem_q)
        results.append(check("删除后语义索引无残留", not any(
            i["type"] == "worklog" and PREFIX in str(i.get("title", ""))
            for i in after["semantic_items"])))

        # 5) 外部索引默认关闭
        backend_status = http(base, "/api/backends/status")
        results.append(check("外部索引默认关闭", backend_status["available"] is False))
        results.append(check("默认无外部结果",
                             not http(base, q_ocr)["plugin_items"]))

        # 6) 检测到 CLI 才启用，且结果独立分组
        cli = tmp / "fake_kensaku.py"
        cli.write_text(PLUGIN_CLI, encoding="utf-8")
        port2 = free_port()
        env2 = dict(os.environ)
        env2["ONLYONE_TOKEN"] = PLUGIN_TOKEN
        env2["ONLYONE_KENSAKU_CLI"] = str(cli)
        plugin_proc = subprocess.Popen(
            [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port2)],
            cwd=str(BACKEND), env=env2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base2 = "http://127.0.0.1:%d" % port2
        if not wait_ready(base2):
            raise RuntimeError("插件测试后端未就绪")
        p_status = http(base2, "/api/backends/status", token=PLUGIN_TOKEN)
        results.append(check("检测到 CLI 后启用", p_status["available"] is True,
                             str(p_status["enabled"])))
        p_res = http(base2, q_ocr, token=PLUGIN_TOKEN)
        plugins = p_res["plugin_items"]
        results.append(check("外部结果独立分组", bool(plugins)
                             and all(i.get("via") == "plugin" for i in plugins),
                             str([i["title"] for i in plugins])))
        results.append(check("外部结果不混进字面组", not any(
            i.get("via") == "plugin" for i in p_res["items"])))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d)" % (sum(results), len(results)),
              flush=True)
        return 0 if ok else 1
    finally:
        if log_id is not None:
            try:
                http(base, "/api/biz/worklogs/%d" % log_id, "DELETE")
            except Exception:  # noqa: BLE001
                pass
        if rid is not None:
            try:
                http(base, "/api/roots/%d" % rid, "DELETE")
            except Exception:  # noqa: BLE001
                pass
        if plugin_proc is not None:
            plugin_proc.terminate()
            try:
                plugin_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                plugin_proc.kill()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        purge()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
