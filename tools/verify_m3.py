# -*- coding: utf-8 -*-
"""M3 验收集：四表 CRUD、统一条目同步、跨类型检索、关联闭环、链接安全、清理。

自带后端进程，不依赖已运行实例；可反复运行（用 ``M3验证-`` 前缀幂等清理）。
用法：py -3 tools\\verify_m3.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

MARK = "M3验证-灾备切换演练"
PREFIX = "M3验证-"
TOKEN = "m3-verify-token-20261106"


def http(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"X-OnlyOne-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def purge(base: str) -> int:
    """删除本脚本创建的全部记录（按前缀识别），成功失败都执行。"""
    removed = 0
    for kind in ("worklogs", "incidents", "links", "projects"):
        try:
            for row in http(base, "/api/biz/" + kind):
                if PREFIX in json.dumps(row, ensure_ascii=False):
                    http(base, "/api/biz/%s/%d" % (kind, row["id"]), "DELETE")
                    removed += 1
        except Exception:  # noqa: BLE001
            pass
    return removed


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""), flush=True)
    return ok


def expect_400(base: str, path: str, body: dict) -> bool:
    try:
        http(base, path, "POST", body)
        return False
    except urllib.error.HTTPError as exc:
        return exc.code == 400


def main() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ)
    env["ONLYONE_TOKEN"] = TOKEN
    proc = subprocess.Popen(
        [sys.executable, str(BACKEND / "run.py"), "--no-window", "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = "http://127.0.0.1:%d" % port
    results: list[bool] = []
    try:
        for _ in range(120):
            try:
                with urllib.request.urlopen(base + "/api/meta/status", timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.15)
        else:
            raise RuntimeError("后端未就绪，检查 data/onlyone.log")

        purged = purge(base)
        if purged:
            print("CLEAN 历史遗留记录", purged, flush=True)

        # 1) 四类业务记录 CRUD 创建，并确认条目同步
        project = http(base, "/api/biz/projects", "POST",
                       {"data": {"name": MARK, "note": MARK + " 项目说明", "status": "active"}})
        log = http(base, "/api/biz/worklogs", "POST",
                   {"data": {"log_date": "2026-11-06", "content": MARK + " 日志内容",
                             "minutes": 45, "project_id": project["id"]}})
        incident = http(base, "/api/biz/incidents", "POST",
                        {"data": {"occurred_at": "2026-11-06 09:00", "system": MARK,
                                  "symptom": "数据库主从切换失败 " + MARK,
                                  "root_cause": "心跳误判", "action": "重启复制链路",
                                  "duration_min": 30, "project_id": project["id"]}})
        link = http(base, "/api/biz/links", "POST",
                    {"data": {"title": MARK, "url": "https://doc.weixin.qq.com/doc/demo",
                              "note": MARK + " 链接备注", "project_id": project["id"]}})
        ids = {"projects": project["id"], "worklogs": log["id"],
               "incidents": incident["id"], "links": link["id"]}
        print("CREATED", ids, flush=True)

        for kind, rid in ids.items():
            rows = http(base, "/api/biz/" + kind)
            target = next((r for r in rows if r["id"] == rid), None)
            results.append(check("CRUD 创建 %s" % kind, target is not None
                                 and bool(target.get("entry_id"))))

        # 2) 一次搜索跨类型命中（FTS5 trigram）
        res = http(base, "/api/search?q=" + urllib.parse.quote("灾备切换演练"))
        types = {i["type"] for i in res["items"]}
        print("SEARCH 跨类型 -> mode=%s hits=%d types=%s" % (res["mode"], len(res["items"]), types),
              flush=True)
        results.append(check("跨类型 FTS5 命中", res["mode"] == "fts5_trigram"
                             and {"project", "worklog", "incident", "link"} <= types,
                             str(sorted(types))))
        results.append(check("命中片段", all(i.get("snippet") for i in res["items"])))

        # 3) 两字中文 LIKE 兜底仍跨类型
        short = http(base, "/api/search?q=" + urllib.parse.quote("演练"))
        results.append(check("短词 LIKE 跨类型", short["mode"] == "like_fallback"
                             and len({i["type"] for i in short["items"]}) >= 4,
                             "hits=%d" % len(short["items"])))

        # 4) 业务记录只读预览返回正文
        items = http(base, "/api/search?q=" + urllib.parse.quote("心跳误判"))["items"]
        if items:
            pv = http(base, "/api/preview?entry_id=%d" % items[0]["id"])
            results.append(check("业务记录预览", "心跳误判" in pv["text"]
                                 and pv["tables"] == [], pv["source"]))
        else:
            results.append(check("业务记录预览", False, "未命中运维记录"))

        # 5) 关联到项目闭环
        logs = http(base, "/api/biz/worklogs")
        log_row = next(r for r in logs if r["id"] == log["id"])
        linked = http(base, "/api/entries/%d/link-project" % log_row["entry_id"], "POST",
                      {"project_id": project["id"]})
        rels = http(base, "/api/relations?entry_id=%d" % project["entry_id"])
        results.append(check("关联到项目", linked["ok"] and any(
            r["peer_id"] == log_row["entry_id"] for r in rels), "relations=%d" % len(rels)))
        dup = http(base, "/api/entries/%d/link-project" % log_row["entry_id"], "POST",
                   {"project_id": project["id"]})
        results.append(check("关联幂等", dup["duplicate"] is True))

        one = http(base, "/api/biz/projects/%d" % project["id"])
        results.append(check("CRUD 单条查询", one["id"] == project["id"]))

        http(base, "/api/entries/%d/link-project?project_id=%d"
             % (log_row["entry_id"], project["id"]), "DELETE")
        rels_empty = http(base, "/api/relations?entry_id=%d" % project["entry_id"])
        results.append(check("解除项目关联", not rels_empty))

        link_row = next(r for r in http(base, "/api/biz/links") if r["id"] == link["id"])
        http(base, "/api/relations", "POST",
             {"src_entry_id": link_row["entry_id"], "dst_entry_id": project["entry_id"],
              "relation": "related"})
        generic = http(base, "/api/relations?entry_id=%d" % project["entry_id"])
        results.append(check("通用关联新增",
                             any(r["peer_id"] == link_row["entry_id"] for r in generic)))
        http(base, "/api/relations?src_entry_id=%d&dst_entry_id=%d&relation=related"
             % (link_row["entry_id"], project["entry_id"]), "DELETE")
        results.append(check("通用关联删除",
                             not http(base, "/api/relations?entry_id=%d" % project["entry_id"])))

        # 6) 更新后 FTS 同步
        http(base, "/api/biz/worklogs/%d" % log["id"], "PUT",
             {"data": {"content": MARK + " 已更新内容XYZ"}})
        updated = http(base, "/api/search?q=" + urllib.parse.quote("已更新内容XYZ"))
        results.append(check("更新同步 FTS", any(
            i["type"] == "worklog" and i["ref_id"] == log["id"] for i in updated["items"])))

        # 7) 删除后条目与关联清理
        http(base, "/api/biz/incidents/%d" % incident["id"], "DELETE")
        after = http(base, "/api/search?q=" + urllib.parse.quote("数据库主从切换失败"))
        results.append(check("删除同步 FTS", not any(i["type"] == "incident" for i in after["items"])))

        # 8) 链接协议白名单（不读取 wedoc 内容）
        results.append(check("链接创建协议校验", expect_400(
            base, "/api/biz/links",
            {"data": {"title": PREFIX + "非法链接", "url": "javascript:alert(1)"}})))
        results.append(check("打开链接协议校验", expect_400(
            base, "/api/open-link", {"url": "javascript:alert(1)"})))

        # 9) 删除项目时解除其余业务记录引用
        http(base, "/api/biz/projects/%d" % project["id"], "DELETE")
        logs_after = http(base, "/api/biz/worklogs")
        stale = [r for r in logs_after if r["id"] == log["id"] and r["project_id"] is not None]
        results.append(check("删除项目解除引用", not stale))

        ok = all(results)
        print(("ALL PASS" if ok else "SOME FAILED") + "  (%d/%d)" % (sum(results), len(results)),
              flush=True)
        return 0 if ok else 1
    finally:
        try:
            purge(base)
        except Exception:  # noqa: BLE001
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("FAIL", type(exc).__name__, exc)
        sys.exit(1)
