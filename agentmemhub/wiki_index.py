# -*- coding: utf-8 -*-
"""Wiki 页面层索引：把 L2 知识页整页投影进 units，参与六路通道召回。

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
import os
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


def _unquote(s: str) -> str:
    """去掉 YAML/JSON 风格的外层引号。

    为什么必须做：frontmatter 的值**允许带引号** —— 投喂链路用 `json.dumps` 写出
    （因为标题里可能有 `:` `#` 引号，裸写会把 frontmatter 写坏）。而这里的正则捕获是
    `\\S+` / `(.+)`，**会连同引号一起吃进去**：实测 `doc: "abc123"` 被解析成 `"abc123"`
    （含引号），与 `materials.db` 的 `doc_id` 对不上 ⇒ **溯源链静默断掉**
    （不报错、不失败，只是查不到）。

    ⚠️ 三处都要（`title` / `origin` / `doc`）—— 我原先以为 `title` 早就 strip 过，
    实测**并没有**（`_TITLE_RE` 的捕获直接 `.strip()` 而已），2026-10-12 一并修。
    """
    s = str(s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse_page(path: Path, *, root: Path | None = None,
               tag: str = "") -> dict[str, Any] | None:
    """解析一个 L2 页面（frontmatter + 摘要 + 正文 + 溯源引用）。

    索引文件（index.md）不是知识页，跳过。

    **三种路径各司其职**（多根改造后必须分开，见 `project_pages`）：
      · `path` —— **展示用**：根内相对路径（人读的"哪个域/哪一页"）
      · `ident` —— **身份用**：`<根标签>::<根内相对路径>`，参与 `src_id`/`seq` 派生
        —— 不同根下同名相对路径**必须不撞**（否则两个根的同名页会互相覆盖）
      · `abs` —— **读全文用**：绝对路径，两阶段召回第二阶段按它打开文件
        （此前存的是根内相对路径，消费者**根本无法直接打开** —— 他不知道根在哪）
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
    title = (_unquote(t.group(1)) if t else path.stem)
    summary = (s.group(1).strip() if s else "")
    # 去掉正文里的 **来源**/**相关** 行与 details 块（它们是元数据，不是知识内容；
    # 留着会让向量被一串编号稀释）
    clean = re.sub(r"^\*\*来源\*\*：.*$", "", body, flags=re.M)
    clean = re.sub(r"^\*\*相关\*\*：.*$", "", clean, flags=re.M)
    clean = re.sub(r"<details>.*?</details>", "", clean, flags=re.S)
    clean = re.sub(r"^#\s+.*$", "", clean, count=1, flags=re.M).strip()
    refs = sorted({int(x) for x in _REF_RE.findall(body)})
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    # 三种路径（见函数 docstring）：展示 / 身份 / 读全文
    if root is not None:
        rel = Path(path).relative_to(root).as_posix()
    else:
        rel = path.name if path.parent.name == "" else f"{path.parent.name}/{path.name}"
    ident = f"{tag}::{rel}" if tag else rel
    # **锚与 seq 都必须由路径派生，不能由内容/位置派生**：
    #  · 内容变 → 页面更新（同一行 UPDATE），不该变成新行 —— 实测踩坑：
    #    src_id 用内容指纹时，页面重编后 hash 变 → 走 INSERT → seq 撞
    #    UNIQUE(source, conversation_id, seq)
    #  · 位置序号（第 i 页）在页面增删后会漂移，同样会撞
    crc = zlib.crc32(ident.encode("utf-8"))
    o = _ORIGIN_RE.search(front)
    doc_m = _DOC_RE.search(front)
    # 值可能带引号（见 `_unquote`）—— 不 strip 会把引号当成 id 的一部分
    origin_val = _unquote(o.group(1)) if o else ""
    doc_val = _unquote(doc_m.group(1)) if doc_m else ""
    is_external = origin_val.strip().lower() in ("external", "feed") \
        or bool(doc_val)
    return {
        "src_id": f"{WIKI_SRC_PREFIX}{crc:08x}",
        "hash": h,
        "seq": -(1_000_000 + (crc % 900_000_000)),
        "title": title,
        "domain": (_unquote(d.group(1)) if d else path.parent.name),
        "summary": summary,
        "body": clean,
        "refs": refs,
        "path": rel,
        "ident": ident,
        "abs": str(Path(path).resolve()),
        "root": tag,
        # 数据来源维度：native=自有记忆沉淀 / external=外部投喂
        "origin": "external" if is_external else "native",
        # 投喂页面的**原件锚**（materials.doc_id）：供召回命中后反查"这是哪份外部
        # 素材"。此前 `_DOC_RE` 解析了它却只用来判 is_external，id 被丢掉 ⇒
        # 溯源链在页面层断掉（M0 地基，2026-10-10）。非投喂页面为 None。
        "doc": (doc_val or None),
    }


def _normalize_roots(roots) -> list[Path]:
    """把"单根或根列表"统一成 `list[Path]`（保持单根调用的向后兼容）。

    顺带**保序去重**：标签是按位置分配的（见 `project_pages`），重复根会让
    标签错位 —— 同一目录被配成两个根时不该扫两遍、也不该抢走别人的标签。
    """
    if isinstance(roots, (str, Path)):
        raw = [Path(roots)]
    else:
        raw = [Path(r) for r in (roots or [])]
    seen: set[str] = set()
    out: list[Path] = []
    for p in raw:
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def root_tag(index: int, root: Path) -> str:
    """根标签：**第一根（主根）不加标签**，其余根用目录名。

    为什么主根不加：主根是历史的 L2（`out_l2_mimo`），它的 `src_id`/`seq` 已经
    躺在库里了。多根改造若给它加前缀，会让**全部既有页面整体重键**
    （旧行被清、新行插入）—— 挂在那些 unit 上的价值分与 👍👎 反馈会变孤儿。
    保持主根身份不变，代价为零、收益为零（它本来就不与谁撞）。

    为什么标签按**配置里的位置**而不是"已存在的根"分配：否则某个根临时不存在时
    另一个根的标签会漂移 ⇒ 身份跟着变 ⇒ 投影错乱。位置由调用方（`page_roots`）
    固定，`live` 只影响"这轮扫不扫"。
    """
    return "" if index == 0 else root.name


def page_roots(l2_dir: Path | str | None = None, *, config=None) -> list[Path]:
    """页面层的**所有根**（**单点定义**）：自有 wiki 的 L2 + 投喂交付物 kb。

    为什么收在一处：CLI / wiki 管道 / 面板三处入口各写一份根集合，必然漂移
    （本项目在"两处流程各写一份"上踩过多次）。加新根只改这里。

    两个根**并列**（用户 2026-10-10 拍板）：`out_l2_mimo` 是自有记忆长出来的，
    `out_feed` 是外部投喂的交付物；它们互不影响，但都在页面层、都能被召回。
    """
    out: list[Path] = []
    if l2_dir:
        out.append(Path(l2_dir))
    try:
        if config is None:
            from agentmemhub.config import config as _cfg
            config = _cfg()
        kb = (config.feed_paths() or {}).get("kb")
        if kb:
            out.append(Path(kb))
    except Exception:                       # noqa: BLE001 —— 配置读不到就只投 L2
        pass
    # 去重（同一目录被配成两个根时不该扫两遍）
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


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


def project_pages(idx: sqlite3.Connection, roots,
                  *, embedder=None, settings=None, log=print) -> dict[str, Any]:
    """全量对齐投影：扫**所有页面根** → upsert units（含向量）→ 删除已不存在的页面。

    幂等且原子：投影后 units 里 source='wiki' 的集合与磁盘目录**精确一致**
    （页面重编/改名/删除都能自动收敛），不会出现新旧版本共存。

    `roots` 可传单个路径或路径列表（**向后兼容单根调用**）。多根的两个要点：

    · **身份不能撞**：`src_id`/`seq` 由 `<根标签>::<根内相对路径>` 派生
      —— 否则两个根下的同名相对路径（如都叫 `01-域A/001-页.md`）会互相覆盖；
    · **清扫只清本轮真扫过的根**：某个根这轮不存在时（目录被移走 / 配置临时变化），
      它的投影**必须留着** —— 不然"根暂时不可用"会把那根的页面全删光、等它回来再重建
      （`out_feed` 与 `out_l2_mimo` 并列时这尤其危险）。
    """
    from agentmemhub.rag.ingest import ensure_bridge_schema
    from agentmemhub.rag.config import load_settings

    dirs = _normalize_roots(roots)
    # **标签按配置位置定**（不是"已存在的根"）—— 否则某个根临时缺席会让另一个根
    # 的标签漂移、`src_id` 跟着变、投影错乱。见 `root_tag()`。
    tags = {str(d): root_tag(i, d) for i, d in enumerate(dirs)}
    live = [d for d in dirs if d.exists()]
    if not live:
        raise FileNotFoundError("页面根都不存在：%s" % [str(d) for d in dirs])
    for missing in [d for d in dirs if d not in live]:
        log("  ⚠️ 页面根不存在，本轮跳过（其既有投影保留）：%s" % missing)
    ensure_bridge_schema(idx)
    st = settings or load_settings()

    pages: list[dict[str, Any]] = []
    skipped_empty = 0
    for root in live:
        tag = tags[str(root)]                # 主根为空串（身份不变），其余用目录名
        for f in sorted(root.rglob("*.md")):
            if f.name == "index.md":
                continue
            # 跳过**点开头目录**下的文件：`.obsidian/`（编辑器配置）与
            # `_swap_domain_dir()` 写盘崩溃时可能残留的 `.tmp_*` / `.bak_*`。
            # 它们是配置或中间态，一旦被当成知识页投影，会以**错误的 domain**
            # （点目录名）混进召回面（2026-09-25 加，与 `_prune_stale_domain_dirs`
            # 的隐藏目录白名单同一原则）。
            if any(p.startswith(".") for p in f.relative_to(root).parts[:-1]):
                continue
            p = parse_page(f, root=root, tag=tag)
            if not p:
                continue
            # **空页面不入召回面**：把产物目录当 Obsidian vault 打开时，点击未解析的
            # `[[链接]]` 会让编辑器在原地**自动新建 0 字节的 md**，文件名就是链接文字。
            # 这类文件会被 parse_page 照收（title 兜底 path.stem、domain 取父目录名），
            # 于是变成"标题看着很正当、正文却是空"的召回单元 —— **安静污染召回面**
            # （2026-09-25 实测：vault 根下 3 个空文件待投影，domain 全是错误的
            #  `out_l2_mimo`，且 origin 会标成 native）。判据用「正文为空」而非文件
            # 大小：正文才是知识，frontmatter 不算。
            if not p["body"].strip():
                skipped_empty += 1
                continue
            pages.append(p)
    if not pages:
        return {"pages": 0, "inserted": 0, "updated": 0, "removed": 0,
                "skipped_empty": skipped_empty,
                "roots": [str(d) for d in live],
                "message": "未找到页面（检查页面根目录）"}

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
                    " origin=?, doc_id=?, updated_at=? WHERE id=?",
                    (p["domain"], p["title"], body_text,
                     len(body_text), p["abs"], p["origin"], p["doc"],
                     t_now, uid))
                updated += 1
            else:
                uid = int(idx.execute(
                    "INSERT INTO units(source, conversation_id, seq, role,"
                    " turn_key, src_id, time, title, text, chars, wiki_path,"
                    " origin, doc_id, updated_at)"
                    " VALUES('wiki',?,?,'wiki',NULL,?,?,?,?,?,?,?,?,?)",
                    (p["domain"], p["seq"], p["src_id"], t_now, p["title"],
                     body_text, len(body_text), p["abs"], p["origin"],
                     p["doc"], t_now)).lastrowid)
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
        # 删除磁盘上已不存在的页面投影（页面被重编/删除后收敛）
        # ⚠️ **只清"本轮真正扫过的根"下的行**：
        #  · 空路径或**相对**路径 = 多根改造前的老数据（当时只有 L2 一个根）⇒ 可清；
        #  · 绝对路径但不在任何已扫根下 = 那个根这轮不存在（被移走/配置临时变化）
        #    ⇒ **留着**，否则"某个根暂时不可用"会把它的投影全删光，等它回来再重建。
        existing = [(r[0], r[1]) for r in idx.execute(
            "SELECT src_id, wiki_path FROM units WHERE source='wiki'")]
        scanned = [str(d.resolve()) for d in live]
        gone: list[str] = []
        for src_id, wiki_path in existing:
            if src_id in seen:
                continue
            wp = str(wiki_path or "")
            if wp and os.path.isabs(wp) and not any(
                    wp.startswith(s) for s in scanned):
                continue
            gone.append(src_id)
        for s in gone:
            row = idx.execute("SELECT id FROM units WHERE src_id=?",
                              (s,)).fetchone()
            if row:
                for s_model, _ in multi:
                    idx.execute(f"DELETE FROM {s_model.vec_table} WHERE rowid=?",
                                (int(row[0]),))
                idx.execute("DELETE FROM units WHERE id=?", (int(row[0]),))
    roots_txt = "、".join(d.name for d in live)
    log(f"页面层投影：{len(pages)} 页（新增 {inserted} / 更新 {updated} / "
        f"清理 {len(gone)}）｜根：{roots_txt}"
        + (f"；跳过 {skipped_empty} 个空页面" if skipped_empty else ""))
    return {"pages": len(pages), "inserted": inserted, "updated": updated,
            "removed": len(gone), "skipped_empty": skipped_empty,
            "roots": [str(d) for d in live]}


def stats(idx: sqlite3.Connection) -> dict[str, Any]:
    """页面层现状（供 align/巡检回答"召回面里有多少页面"）。"""
    n = idx.execute("SELECT COUNT(*) FROM units WHERE source='wiki'").fetchone()[0]
    return {"pages_in_index": int(n)}
