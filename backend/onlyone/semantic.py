# -*- coding: utf-8 -*-
"""语义 / 向量检索（M6-2）：本地确定性稀疏向量，零模型、零云端、零额外依赖。

不引入神经嵌入模型（本机 Python 3.14 无 torch / onnxruntime 可用发行版，且单机离线
不允许调用在线模型）。实现方式是：

* **同义词归一**：把「宕机 / 服务不可用 / 崩溃」等近义表达映射到同一个规范项，
  这是「语义相近但没有字面命中」能被找到的关键；
* **字符 n-gram + 英文词**：补充字面之外的模糊相似；
* **L2 归一稀疏向量 + 余弦点积**：存 SQLite，查询时全表打分（单机单人规模足够）。

结果以**独立分组**返回（``semantic_items``），不与 FTS5 的字面命中混排——两套分数
不可直接比较。若将来接入句子嵌入模型，只需替换 ``embed()``，表结构与调用方不变。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter

# 近义组：组内所有说法映射到组首词的规范项
SYNONYM_GROUPS = (
    ("灾备", "容灾", "灾难恢复", "故障恢复", "应急恢复"),
    ("宕机", "服务不可用", "停机", "崩溃", "挂了", "不可用"),
    ("数据库", "db", "资料库"),
    ("备份", "快照", "副本", "冷备"),
    ("运维", "运营维护", "系统维护"),
    ("故障", "异常", "告警", "报错", "事故"),
    ("切换", "倒换", "主备切换"),
    ("恢复", "还原", "回退", "回滚"),
    ("手册", "指南", "文档", "说明"),
    ("项目", "专项", "任务"),
    ("密码", "口令", "凭据", "credential"),
)

CANON: dict[str, str] = {}
for _group in SYNONYM_GROUPS:
    _key = "SYN:" + _group[0]
    for _word in _group:
        CANON[_word.lower()] = _key

MIN_SCORE = 0.15
MAX_SEMANTIC_RESULTS = 20
SYNONYM_WEIGHT = 3.0
# 词袋向量对超长正文不敏感，但向量 JSON 会线性膨胀（实测 34.7 万字 → 31KB）。
# 正文分片索引后单文件可达数百万字，这里只取前 400k 参与向量化，控制 semantic_index 体积。
SEMANTIC_MAX_CHARS = 400_000


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    tokens = [canon for word, canon in CANON.items() if word in lowered]
    compact = re.sub(r"\s+", "", lowered)
    tokens.extend(compact[i:i + 2] for i in range(len(compact) - 1))
    tokens.extend(re.findall(r"[a-z0-9]{2,}", lowered))
    return tokens


def embed(text: str) -> dict[str, float]:
    """生成 L2 归一的稀疏向量。"""
    counts = Counter(_tokens(text))
    vec: dict[str, float] = {}
    for term, count in counts.items():
        weight = SYNONYM_WEIGHT if term.startswith("SYN:") else 1.0
        vec[term] = (1 + math.log(count)) * weight
    norm = math.sqrt(sum(value * value for value in vec.values())) or 1.0
    return {term: round(value / norm, 6) for term, value in vec.items()}


def sync_entry(conn: sqlite3.Connection, entry_id: int,
               title: str, body: str) -> None:
    """条目写入 / 更新时同步向量（标题加权 ×2）。"""
    text = (body or "")[:SEMANTIC_MAX_CHARS]
    vector = embed("%s %s\n%s" % (title or "", title or "", text))
    conn.execute(
        """INSERT INTO semantic_index(entry_id,vector,updated_at)
           VALUES(?,?,datetime('now','localtime'))
           ON CONFLICT(entry_id) DO UPDATE SET
             vector=excluded.vector, updated_at=excluded.updated_at""",
        (entry_id, json.dumps(vector, ensure_ascii=False)),
    )


def drop_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM semantic_index WHERE entry_id=?", (entry_id,))


def _safe_term(term: str) -> bool:
    """term 在向量 JSON 的 key 中是否不需要转义（与原文逐字一致）。"""
    return bool(term) and all(ch.isalnum() or ch in ":.-" for ch in term)


def _term_pattern(query_vector: dict[str, float]):
    """构造只匹配查询 term 的正则；term 含 JSON 转义字符时返回 None（退化路径）。

    向量以 ``json.dumps(..., ensure_ascii=False)`` 存储，形如 ``{"晶盛": 0.123}``。
    term 都是安全字符时，一次 C 层正则扫描即可取出所需数值，无需把每行
    848 个键整体构造成 dict（本机 6292 行 / 87MB 文本实测 3.0~4.8s → 0.12~0.42s）。
    """
    keys = list(query_vector)
    if not keys or not all(_safe_term(k) for k in keys):
        return None
    return re.compile('"(' + '|'.join(re.escape(k) for k in keys)
                      + ')":\\s*([-+0-9.eE]+)')


def search(conn: sqlite3.Connection, q: str,
           exclude_ids: set[int] | tuple[int, ...] = (),
           limit: int = MAX_SEMANTIC_RESULTS) -> list[tuple[int, float]]:
    """返回 ``(entry_id, score)``，排除字面检索已命中的条目。"""
    query_vector = embed(q)
    if not query_vector:
        return []
    excluded = set(exclude_ids)
    pattern = _term_pattern(query_vector)
    scored: list[tuple[int, float]] = []
    for row in conn.execute("SELECT entry_id, vector FROM semantic_index"):
        entry_id = row["entry_id"]
        if entry_id in excluded:
            continue
        try:
            if pattern is not None:
                found = {m.group(1): float(m.group(2))
                         for m in pattern.finditer(row["vector"] or "")}
                # 与 json.loads 路径同样的累加顺序，保证浮点结果完全一致
                score = sum(weight * found.get(term, 0.0)
                            for term, weight in query_vector.items())
            else:
                vector = json.loads(row["vector"])
                score = sum(weight * vector.get(term, 0.0)
                            for term, weight in query_vector.items())
        except (TypeError, ValueError):
            continue
        if score >= MIN_SCORE:
            scored.append((entry_id, round(score, 4)))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]
