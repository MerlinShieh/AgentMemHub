# -*- coding: utf-8 -*-
"""Wiki 页面层索引：把 L2 知识页整页投影进 units，参与三路召回。

为什么要这一层
==============
wiki 页面此前是**只读产物**——Agent 搜不到它（召回面只有消息层/记忆层/手写层）。
但 L2 页面是知识密度最高的形态（跨会话聚合、矛盾已标注、互相链接），
不参与召回等于知识库对 Agent 不可见。

为什么**整页投影、不切片**
==========================
L2 页面本身就是 LLM 提炼后的"一个主题一页"——它已经是最优召回粒度。
再切回碎片等于把整合过的知识拆散（wiki 是切片整合的产物，切回去是反向做功）。
实测：L2 覆盖了 98.7% 的 L1 内容，L1 只是中间态，不进召回面（只作下钻）。

代表什么
========
· source='wiki'、role='wiki'、conversation_id=主题域（可按域筛选/限席）
· src_id='wiki_<内容指纹>'——内容变即新行；**全量对齐**保证同一页面
  不会新旧两版共存（投影是原子的：扫目录 → upsert → 删目录中已不存在的）
· text=标题+摘要+正文（供向量与 FTS）；wiki_path=页面相对路径（两阶段召回的
  第二阶段：需要细节时按路径读全文）
· 向量按 write_order 多模型写入（与 ingest/import_bundle 同语义）
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any

#: 页面正文里的全局记忆引用（溯源锚之一：指向蒸馏记忆）
_REF_RE = re.compile(r"\[m(\d+)\]")
_SUM_RE = re.compile(r"^\*\*摘要\*\*：(.+)$", re.M)
_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.M)
_DOMAIN_RE = re.compile(r"^domain:\s*(.+)$", re.M)
#: 来源维度：投喂编译的页面在 frontmatter 标 `origin: external`（可带 doc id）。
#: 缺省 = native（自有记忆沉淀长出来的页面）。
_ORIGIN_RE = re.compile(r"^origin:\s*(\S+)$", re.M)
_DOC_RE = re.compile(r"^doc:\s*(\S+)$", re.M)

WIKI_SRC_PREFIX = "wiki_"


def parse_page(path: Path) -> dict[str, Any] | None:
    """解析一个 L2 页面（frontmatter + 摘要 + 正文 + 溯源引用）。

    索引文件（index.md）不是知识页，跳过。
    """
    if path.name == "index.md":
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    head = text.split("---", 2)
    front = head[1] if len(head) >= 3 else ""
    body = head[2] if len(head) >= 3 else text
    t = _TITLE_RE.search(front)
    d = _DOMAIN_RE.search(front)
    s = _SUM_RE.search(body[:2000])
    title = (t.group(1).strip() if t else path.stem)
    summary = (s.group(1).strip() if s else "")
    # 去掉正文里的 **来源**/**相关** 行与 details 块（它们是元数据，不是知识内容；
    # 留着会让向量被一串编号稀释）
    clean = re.sub(r"^\*\*来源\*\*：.*$", "", body, flags=re.M)
    clean = re.sub(r"^\*\*相关\*\*：.*$", "", clean, flags=re.M)
    clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S)
    clean = re.sub(r"^#\s+.*$", "", clean, count=1, flags=re.M).strip()
    refs = sorted({int(x) for x in _REF_RE.findall(body)})
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    rel = path.name if path.parent.name == "" else f"{path.parent.name}/{path.name}"
    # **锚与 seq 都必须由路径派生，不能由内容/位置派生**：
    #  · 内容变 → 页面更新（同一行 UPDATE），不该变成新行 —— 实测踩坑：
    #    src_id 用内容指纹时，页面重编后 hash 变 → 走 INSERT → seq 撞
    #    UNIQUE(source, conversation_id, seq)
    #  · 位置序号（第 i 页）在页面增删后会漂移，同样会撞
    crc = zlib.crc32(rel.encode("utf-8"))
    o = _ORIGIN_RE.search(front)
    doc_m = _DOC_RE.search(front)
    is_external = bool(o and o.group(1).strip().lower() in ("external", "feed")) \
        or bool(doc_m)
    return {
        "src_id": f"{WIKI_SRC_PREFIX}{crc:08x}",
        "hash": h,
        "seq": -(1_000_000 + (crc % 900_000_000)),
        "title": title,
        "domain": (d.group(1).strip() if d else path.parent.name),
        "summary": summary,
        "body": clean,
        "refs": refs,
        "path": rel,
        # 数据来源维度：native=自有记忆沉淀 / external=外部投喂
        "origin": "external" if is_external else "native",
    }


def _encode_pages(conn: sqlite3.Connection, st, texts: list[str],
                  embedder=None) -> list[tuple[Any, Any]]:
    """按 write_order 逐模型编码页面文本（多写一份消除换模型风险）。

    与 ingest / import_bundle 同语义。抽成独立函数是为了可注入替身——
    测试不必加载真实 embedding 模型。
    """
    from agentmemhub.rag.ingest import ensure_vec_table
    from agentmemhub.rag.runtime import get_embedder

    out: list[tuple[Any, Any]] = []
    for mid in st.write_order:
        s = st.model(mid)
        ensure_vec_table(conn, s)
        em = (embedder if embedder is not None and mid == st.active_model
              else get_embedder(s, settings=st))
        out.append((s, em.encode_passages(texts)))
    return out


def project_pages(idx: sqlite3.Connection, l2_dir: Path | str,
                  *, embedder=None, settings=None, log=print) -> dict[str, Any]:
    """全量对齐投影：扫 L2 域目录 → upsert units（含向量）→ 删除已不存在的页面。

    幂等且原子：投影后 units 里 source='wiki' 的集合与磁盘目录**精确一致**
    （页面重编/改名/删除都能自动收敛），不会出现新旧版本共存。
    """
    from agentmemhub.rag.ingest import ensure_bridge_schema
    from agentmemhub.rag.config import load_settings

    root = Path(l2_dir)
    if not root.exists():
        raise FileNotFoundError(f"L2 产出目录不存在：{root}")
    ensure_bridge_schema(idx)
    st = settings or load_settings()

    pages: list[dict[str, Any]] = []
    for f in sorted(root.rglob("*.md")):
        if f.name == "index.md":
            continue
        p = parse_page(f)
        if p:
            pages.append(p)
    if not pages:
        return {"pages": 0, "inserted": 0, "updated": 0, "removed": 0,
                "message": "未找到页面（检查 L2 目录）"}

    # 向量用**整页正文**（含标题与摘要）：实测比"仅标题+摘要"更好——摘要虽聚焦，
    # 却丢了正文里的关键词（查询"容错语义"时，核心页从 0.826 掉出榜单）。
    # 全文进 FTS 与向量、摘要单独返回，各司其职。
    multi = _encode_pages(
        idx, st, [("%s\n\n%s" % (p["title"], p["body"]))[:6000] for p in pages],
        embedder)

    inserted = updated = 0
    seen: set[str] = set()
    t_now = int(time.time())
    with idx:
        for i, p in enumerate(pages):
            seen.add(p["src_id"])
            body_text = (p["title"] + "\n\n"
                         + (f"摘要：{p['summary']}\n\n" if p["summary"] else "")
                         + p["body"])
            row = idx.execute("SELECT id FROM units WHERE src_id=?",
                              (p["src_id"],)).fetchone()
            if row:
                uid = int(row[0])
                idx.execute(
                    "UPDATE units SET source='wiki', conversation_id=?,"
                    " role='wiki', title=?, text=?, chars=?, wiki_path=?,"
                    " origin=?, updated_at=? WHERE id=?",
                    (p["domain"], p["title"], body_text,
                     len(body_text), p["path"], p["origin"], t_now, uid))
                updated += 1
            else:
                uid = int(idx.execute(
                    "INSERT INTO units(source, conversation_id, seq, role,"
                    " turn_key, src_id, time, title, text, chars, wiki_path,"
                    " origin, updated_at)"
                    " VALUES('wiki',?,?,'wiki',NULL,?,?,?,?,?,?,?,?)",
                    (p["domain"], p["seq"], p["src_id"], t_now, p["title"],
                     body_text, len(body_text), p["path"], p["origin"],
                     t_now)).lastrowid)
                inserted += 1
            for s, vecs in multi:
                # vec0 虚拟表**不支持 INSERT OR REPLACE 的主键替换语义**
                # （同一 rowid 重复插入抛 UNIQUE 冲突）——原地更新必须先删。
                # 这个坑在 distill._project_one 里踩过一次，这里同样适用。
                idx.execute(f"DELETE FROM {s.vec_table} WHERE rowid=?", (uid,))
                idx.execute(
                    f"INSERT INTO {s.vec_table}(rowid, embedding) VALUES(?,?)",
                    (uid, bytes(vecs[i])))
            # 来源初始分：页面是人工精选/LLM 聚合出的高密度知识，给与
            # "Agent 主动写入"同档的起点（0.6），参与价值加权——否则它在
            # 融合排序里会因为"长文本相似度天然偏低"被短条目压制
            try:
                from agentmemhub.rag.memstore import (
                    BASE_VALUE_AGENT_WRITE, ensure_base_value)
                ensure_base_value(idx, uid, BASE_VALUE_AGENT_WRITE)
            except Exception:
                pass
        # 删除目录中已不存在的页面投影（页面被重编/删除后收敛）
        existing = [r[0] for r in idx.execute(
            "SELECT src_id FROM units WHERE source='wiki'")]
        gone = [s for s in existing if s not in seen]
        for s in gone:
            row = idx.execute("SELECT id FROM units WHERE src_id=?",
                              (s,)).fetchone()
            if row:
                for s_model, _ in multi:
                    idx.execute(f"DELETE FROM {s_model.vec_table} WHERE rowid=?",
                                (int(row[0]),))
                idx.execute("DELETE FROM units WHERE id=?", (int(row[0]),))
    log(f"页面层投影：{len(pages)} 页（新增 {inserted} / 更新 {updated} / "
        f"清理 {len(gone)}）")
    return {"pages": len(pages), "inserted": inserted, "updated": updated,
            "removed": len(gone)}


def stats(idx: sqlite3.Connection) -> dict[str, Any]:
    """页面层现状（供 align/巡检回答"召回面里有多少页面"）。"""
    n = idx.execute("SELECT COUNT(*) FROM units WHERE source='wiki'").fetchone()[0]
    return {"pages_in_index": int(n)}
