"""wiki 路径的冲突解析测试：从 L2 页面正文提取矛盾/取代标注。

这是「语义级判断的沉淀」——L2 编译时 LLM 已经判断过（`_PAGE_RULES` 规则 4/5），
本模块只把结论解析成结构化数据，**不改任何产出**。

真实数据（2026-11-07，1361 个 L2 页面）：结构化标注 187 条 → 可落表 102 条
（54.5%）、编号过多待复核 26 条、无法解析 46 条、澄清类 13 条。
"""
from __future__ import annotations

import sqlite3

import pytest

from agentmemhub import conflicts as cf
from agentmemhub.rag.ingest import ensure_vec_table, open_index


def _write_page(root, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ── 解析：只认引用块行首的 ⚠️ 标注 ─────────────────────────────────────

def test_parse_only_anchored_callouts(tmp_path):
    """页面摘要里的"矛盾"是叙述用词，不是标注——不能算。"""
    _write_page(tmp_path, "01-域/001-页.md", """
# 标题

摘要：本文分析了内部数据矛盾与规则自相矛盾的问题。

> ⚠️ 矛盾：发布记录称 tag 在 6b657c6 [m4350][m4351]，而审计记录称 fc983a4 [m4352]。

## 正文

这里又说了一遍矛盾这个词，但没有引用块前缀。
""")
    anns = cf.parse_wiki_annotations(tmp_path)
    assert len(anns) == 1, "只有行首 `> ⚠️ 矛盾：` 才算标注"
    assert anns[0]["kind"] == "conflict"
    assert anns[0]["refs"] == [4350, 4351, 4352]


def test_parse_supersede_kind(tmp_path):
    """`取代关系` 映射为 evolution。"""
    _write_page(tmp_path, "01-域/001-页.md",
                "> ⚠️ 取代关系：早期用 Python 双连接 [m4363]；改为 SQL ATTACH [m4370]。\n")
    anns = cf.parse_wiki_annotations(tmp_path)
    assert anns[0]["kind"] == "evolution"
    assert anns[0]["refs"] == [4363, 4370]


def test_parse_does_not_cross_lines(tmp_path):
    """回归：正则空白必须用 `[ \\t]*` —— `\\s*` 会跨行把下一行内容当标注正文。

    实测踩到：标注正文里混进了 `> - 事实：…` 这种续行，导致编号归属全错。
    """
    _write_page(tmp_path, "01-域/001-页.md",
                "> ⚠️ 矛盾：这条是空的编号\n"
                "> - 事实：`Bearer` 单独使用返回 401 [m2665]。\n")
    anns = cf.parse_wiki_annotations(tmp_path)
    # 第一行正文是"这条是空的编号"（无编号），第二行不该被拼进来
    assert len(anns) == 1
    assert anns[0]["refs"] == [], "不得把下一行的 [m2665] 算进本条标注"


def test_parse_skips_index_md(tmp_path):
    """index.md 是目录索引，不解析。"""
    _write_page(tmp_path, "index.md",
                "> ⚠️ 矛盾：目录页里不该有这种标注但即使有也跳过 [m1][m2]\n")
    _write_page(tmp_path, "01-域/001-页.md",
                "> ⚠️ 矛盾：真页面 [m3][m4]\n")
    anns = cf.parse_wiki_annotations(tmp_path)
    assert len(anns) == 1 and anns[0]["refs"] == [3, 4]


def test_parse_marks_clarified(tmp_path):
    """澄清句（"并非矛盾/实为一致"）要被标记出来，调用方应跳过。"""
    _write_page(tmp_path, "01-域/001-页.md",
                "> ⚠️ 矛盾：记忆 [m1] 与 [m2] 描述同一事件，实为一致。\n")
    anns = cf.parse_wiki_annotations(tmp_path)
    assert anns[0]["clarified"] is True


def test_parse_missing_dir_returns_empty(tmp_path):
    assert cf.parse_wiki_annotations(tmp_path / "nope") == []


# ── 落表：方向判定与各类跳过 ───────────────────────────────────────────

def _mk_index(tmp_path, project_settings):
    import dataclasses
    from agentmemhub.distill import ensure_distill_schema
    from agentmemhub.rag.ingest import ensure_vec_table, open_index

    active = project_settings.active_spec
    st = dataclasses.replace(project_settings,
                            index_db=tmp_path / "idx_wiki_parse.db")
    conn = open_index(st.index_db)
    ensure_vec_table(conn, active)
    ensure_distill_schema(conn)          # distilled_memories（[mN] 的正文来源）
    return st, conn


def _add_mem(conn, mem_id: int, *, created: int, text: str,
             src_prefix: str = "dst") -> int:
    """造一条蒸馏记忆 + 它的 units 投影（id 可指定，模拟 [m<id>]）。"""
    h = f"hash{mem_id:04d}"
    with conn:
        conn.execute(
            "INSERT INTO distilled_memories(id, source, conversation_id,"
            " content_hash, type, topic, content, confidence, status,"
            " prompt_ver, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mem_id, "s", "c", h, "fact", "t", text, "high", "new", 1, created))
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, title, text, chars)"
            " VALUES('memory','c',?,'distilled','tk',?,?, 't', ?, ?)",
            (mem_id, f"{src_prefix}_{h}", created, text, len(text)))
    return cur.lastrowid


def test_ingest_sets_direction_by_time(tmp_path, project_settings):
    """方向：记忆时间早的为 old、晚的为 new（L2 正文不保证顺序）。"""
    _, conn = _mk_index(tmp_path, project_settings)
    try:
        old_uid = _add_mem(conn, 101, created=1_700_000_000, text="旧的结论")
        new_uid = _add_mem(conn, 102, created=1_800_000_000, text="新的结论")
        l2 = tmp_path / "l2"
        _write_page(l2, "01-域/001-页.md",
                    "> ⚠️ 取代关系：早期 [m102] 后来 [m101]\n")  # 故意反序写
        st = cf.ingest_wiki_annotations(conn, l2)
        assert st["inserted"] == 1
        row = conn.execute(
            "SELECT unit_new, unit_old, kind, rule_hit FROM memory_conflicts"
        ).fetchone()
        assert row[0] == new_uid and row[1] == old_uid, "按时间判定方向，不看书写顺序"
        assert row[2] == "evolution"
        assert row[3].startswith("wiki:")
    finally:
        conn.close()


def test_ingest_skips_ambiguous_and_unresolved(tmp_path, project_settings):
    """3+ 编号 → ambiguous（不可靠）；单编号 → unresolved；澄清 → clarified。"""
    _, conn = _mk_index(tmp_path, project_settings)
    try:
        for mid in (201, 202, 203, 204):
            _add_mem(conn, mid, created=1_700_000_000 + mid, text=f"记忆{mid}")
        l2 = tmp_path / "l2"
        _write_page(l2, "01-域/001-页.md",
                    "> ⚠️ 矛盾：三条证据 [m201][m202][m203]\n"
                    "> ⚠️ 矛盾：只有一条 [m204]\n"
                    "> ⚠️ 矛盾：这两条实为一致 [m201][m202]\n")
        st = cf.ingest_wiki_annotations(conn, l2)
        assert st["ambiguous"] == 1
        assert st["unresolved"] == 1
        assert st["clarified"] == 1
        assert st["inserted"] == 0
    finally:
        conn.close()


def test_ingest_is_idempotent(tmp_path, project_settings):
    """同一对重复标注不重复落表（UNIQUE pair）。"""
    _, conn = _mk_index(tmp_path, project_settings)
    try:
        _add_mem(conn, 301, created=1_700_000_000, text="旧")
        _add_mem(conn, 302, created=1_800_000_000, text="新")
        l2 = tmp_path / "l2"
        _write_page(l2, "01-域/001-页.md", "> ⚠️ 取代关系：[m301][m302]\n")
        _write_page(l2, "02-域/002-页.md", "> ⚠️ 取代关系：[m301][m302]\n")
        first = cf.ingest_wiki_annotations(conn, l2)
        assert first["inserted"] == 1 and first["skipped"] == 1, \
            "同一对在两个页面被标注，只落一条"
        second = cf.ingest_wiki_annotations(conn, l2)
        assert second["inserted"] == 0
        assert cf.stats(conn)["total"] == 1
    finally:
        conn.close()


def test_ingest_reports_unmapped_memory(tmp_path, project_settings):
    """记忆存在但 units 投影缺失 → 计入 unresolved（实测占 46/187 的一部分）。"""
    _, conn = _mk_index(tmp_path, project_settings)
    try:
        with conn:
            conn.execute(
                "INSERT INTO distilled_memories(id, source, conversation_id,"
                " content_hash, type, topic, content, confidence, status,"
                " prompt_ver, created_at) VALUES(401,'s','c','h401','fact',"
                "'t','无投影的记忆','high','new',1,1700000000)")
        _add_mem(conn, 402, created=1_800_000_000, text="有投影")
        l2 = tmp_path / "l2"
        _write_page(l2, "01-域/001-页.md", "> ⚠️ 矛盾：[m401][m402]\n")
        st = cf.ingest_wiki_annotations(conn, l2)
        assert st["inserted"] == 0
        assert st["fetched_unmapped"] == 1
        assert st["unresolved"] == 1
    finally:
        conn.close()
