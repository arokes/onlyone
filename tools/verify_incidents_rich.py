# -*- coding: utf-8 -*-
"""运维记录富文本 + 图片粘贴的回归：ProseMirror JSON 存取、FTS 取纯文本、图片池。

用法：py -3 tools\\verify_incidents_rich.py

覆盖点
------
1. ``POST /api/attachments/images`` 上传 → ``GET /api/attachments/blob/..`` 可取回，同图去重。
2. 非图片 / 超限数据被拒。
3. 运维记录四个富文本字段存 JSON 后可原样取回（编辑器能 setContent）。
4. 纯文本老数据照样能读、能检索（不做迁移）。
5. FTS 正文 / 条目标题 / 搜索摘要里不出现 JSON 结构与图片 URL。
6. 项目工作台预览的 symptom 是纯文本。
"""
from __future__ import annotations

import base64
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

DATA_DIR = Path(tempfile.mkdtemp(prefix="onlyone_incident_"))
os.environ["ONLYONE_DATA_DIR"] = str(DATA_DIR)

TOKEN = "incident-rich-token-20261123"
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


def http(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def raw_get(base: str, path: str):
    req = urllib.request.Request(base + path, headers={"X-OnlyOne-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read(), resp.headers.get("content-type", "")


def expect_status(base: str, path: str, method: str, body, expected: int) -> bool:
    try:
        http(base, path, method, body)
        return False
    except urllib.error.HTTPError as exc:
        return exc.code == expected


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def doc(*paragraphs: str, image_url: str = "") -> str:
    content: list[dict] = []
    for text in paragraphs:
        content.append({"type": "paragraph",
                        "content": [{"type": "text", "text": text}] if text else []})
    if image_url:
        content.append({"type": "image", "attrs": {"src": image_url}})
    return json.dumps({"type": "doc", "content": content}, ensure_ascii=False)


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
    incident_ids: list[int] = []
    project_id = 0
    try:
        for _ in range(160):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        else:
            raise RuntimeError("后端未就绪")

        # 1) 图片池：上传 / 去重 / 取回
        payload = {"data": "data:image/png;base64," + PNG_1PX, "name": "shot.png"}
        up = http(base, "/api/attachments/images", "POST", payload)
        results.append(check("图片上传返回内容寻址 URL",
                             up["url"].startswith("/api/attachments/blob/")
                             and up["dedup"] is False, up["url"]))
        again = http(base, "/api/attachments/images", "POST", payload)
        results.append(check("同一张图重复上传命中去重", again["dedup"] is True))
        status, body, ctype = raw_get(base, up["url"])
        results.append(check("blob 可取回且 Content-Type 为 png",
                             status == 200 and body == base64.b64decode(PNG_1PX)
                             and "png" in ctype, ctype))
        results.append(check("非图片 data URL 被拒",
                             expect_status(base, "/api/attachments/images", "POST",
                                           {"data": "data:text/plain;base64,aGk="}, 400)))
        results.append(check("非法 blob 文件名被拒",
                             expect_status(base, "/api/attachments/blob/..%2Fonlyone.db",
                                           "GET", None, 400)
                             or expect_status(base, "/api/attachments/blob/zzz.png",
                                              "GET", None, 400)))

        # 2) 富文本记录：四个字段存 JSON
        project = http(base, "/api/biz/projects", "POST", {"data": {"name": "回归项目-富文本"}})
        project_id = project["id"]
        rich_symptom = doc("SAP MRP 需求突然翻倍", "09:00 起持续", image_url=up["url"])
        created = http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": "2026-11-23 09:00",
            "system": "SAP-PP",
            "proposer": "张工", "company": "信息中心",
            "symptom": rich_symptom,
            "root_cause": doc("计划边际码配错"),
            "action": doc("调整 MRP 类型后重跑"),
            "note": doc("附图见现象"),
            "project_id": project_id,
        }})
        incident_ids.append(created["id"])
        row = http(base, f"/api/biz/incidents/{created['id']}")
        results.append(check("四个富文本字段原样存取",
                             row["symptom"] == rich_symptom
                             and json.loads(row["root_cause"])["type"] == "doc"))
        results.append(check("提出人 / 公司 可存取",
                             row["proposer"] == "张工" and row["company"] == "信息中心",
                             "%s/%s" % (row.get("proposer"), row.get("company"))))
        by_company = http(base, "/api/search?q=" + urllib.parse.quote("信息中心"))
        results.append(check("公司 / 提出人 参与全文检索",
                             any(it.get("ref_id") == created["id"]
                                 for it in by_company["items"])))
        results.append(check("工作日志接口已下线",
                             expect_status(base, "/api/biz/worklogs", "GET", None, 404)))

        # 3) 老数据：纯文本列值不迁移也能读
        legacy = http(base, "/api/biz/incidents", "POST", {"data": {
            "occurred_at": "2026-11-20 08:00",
            "system": "SAP-MM",
            "symptom": "库存报表口径不一致",
            "root_cause": "视图版本过旧",
        }})
        incident_ids.append(legacy["id"])
        lrow = http(base, f"/api/biz/incidents/{legacy['id']}")
        results.append(check("纯文本老数据原样返回", lrow["symptom"] == "库存报表口径不一致"))

        # 4) 检索：命中富文本内容，且摘要/标题不含 JSON 与图片 URL
        search = http(base, "/api/search?q=" + urllib.parse.quote("需求突然翻倍"))
        hit = next((it for it in search["items"] if it.get("ref_id") == created["id"]), None)
        results.append(check("FTS 能按富文本正文命中", hit is not None))
        if hit:
            blob = json.dumps(hit, ensure_ascii=False)
            clean = ('"type":"doc"' not in blob and "api/attachments" not in blob
                     and "content" not in hit.get("title", ""))
            results.append(check("搜索结果不含 JSON 结构与图片 URL", clean, hit.get("title", "")))
            results.append(check("条目标题取纯文本",
                                 hit["title"].startswith("SAP-PP · SAP MRP"),
                                 hit["title"]))
        legacy_hit = http(base, "/api/search?q=" + urllib.parse.quote("库存报表口径"))
        results.append(check("纯文本老数据仍可检索",
                             any(it.get("ref_id") == legacy["id"]
                                 for it in legacy_hit["items"])))

        # 5) 项目工作台预览：symptom 为纯文本
        ws = http(base, f"/api/projects/{project_id}/workspace")
        preview = next((p for p in ws["previews"]["incidents"] if p["id"] == created["id"]), None)
        results.append(check("项目工作台预览 symptom 已抽成纯文本",
                             preview is not None
                             and preview["symptom"].startswith("SAP MRP 需求突然翻倍")
                             and "type" not in preview["symptom"]))

        # 6) 更新与删除
        http(base, f"/api/biz/incidents/{created['id']}", "PUT", {"data": {
            "symptom": doc("SAP MRP 需求突然翻倍（已定位）"),
        }})
        search2 = http(base, "/api/search?q=" + urllib.parse.quote("已定位"))
        results.append(check("更新后 FTS 同步新正文",
                             any(it.get("ref_id") == created["id"] for it in search2["items"])))
        http(base, f"/api/biz/incidents/{created['id']}", "DELETE", None)
        incident_ids.remove(created["id"])
        results.append(check("删除记录后接口不再返回",
                             expect_status(base, f"/api/biz/incidents/{created['id']}",
                                           "GET", None, 404)))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d)" % (sum(results), len(results)),
              flush=True)
        return 0 if ok else 1
    finally:
        for rid in incident_ids:
            try:
                http(base, f"/api/biz/incidents/{rid}", "DELETE", None)
            except Exception:  # noqa: BLE001
                pass
        if project_id:
            try:
                http(base, f"/api/biz/projects/{project_id}", "DELETE", None)
            except Exception:  # noqa: BLE001
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
