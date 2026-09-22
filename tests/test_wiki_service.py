# -*- coding: utf-8 -*-
"""LLM Wiki 服务层测试：失败清单查询 + 定向补跑入口。

这一层的意义是"**补跑不该只能靠人敲脚本**"——CLI / 面板 / 其它程序都走它。
所以测的重点是**接口契约**：

  · 查询返回可序列化的结构化数据（面板直接能吐给前端）
  · 没有失败项时给明确答复，而不是静默返回空
  · `quota` / `auth` 这类致命错误必须被标出来（重试无意义，得先让人处理）
  · 参数不合法时明确拒绝，而不是跑一半才炸
"""
from __future__ import annotations

import pytest

from agentmemhub import wiki
from agentmemhub.failures import FailureLog


def _mk(tmp_path, entries):
    out = tmp_path / "l2"
    out.mkdir(parents=True, exist_ok=True)
    fl = FailureLog(out / "failures.jsonl")
    for e in entries:
        fl.record(**e)
    return out


# ---------------------------------------------------------------------------
# 查：failed_summary
# ---------------------------------------------------------------------------

def test_摘要_没有清单时给出明确标记(tmp_path):
    s = wiki.failures_summary(tmp_path / "nope")
    assert s["exists"] is False
    assert s["total"] == 0
    assert s["needs_manual"] is False


def test_摘要_按原因分类(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "qwen/a", "error": "TimeoutError: timed out"},
        {"stage": "l1", "target": "qwen/b", "error": "模型返回空内容"},
        {"stage": "l2-compile", "target": "域X", "error": "无法从模型输出解析 JSON"},
    ])
    s = wiki.failures_summary(out)
    assert s["total"] == 3
    assert s["by_kind"]["transient"] == 1
    assert s["by_kind"]["format"] == 2


def test_摘要_致命错误被标出并置needs_manual(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "a", "error": "HTTP 402 insufficient balance"},
        {"stage": "l1", "target": "b", "error": "timeout"},
    ])
    s = wiki.failures_summary(out)
    assert s["needs_manual"] is True
    assert "quota" in s["fatal"]


def test_摘要_按阶段计数(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "a", "error": "timeout"},
        {"stage": "l1", "target": "b", "error": "timeout"},
        {"stage": "l2-plan", "target": "域A", "error": "timeout"},
    ])
    s = wiki.failures_summary(out)
    assert s["stages"]["l1"] == 2
    assert s["stages"]["l2-plan"] == 1
    assert s["stages"]["l2-compile"] == 0


def test_摘要_可JSON序列化(tmp_path):
    """面板要直接把它吐给前端，不能有不可序列化的东西。"""
    import json
    out = _mk(tmp_path, [{"stage": "l1", "target": "a", "error": "timeout"}])
    json.dumps(wiki.failures_summary(out), ensure_ascii=False)


def test_摘要_可按阶段过滤(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "a", "error": "timeout"},
        {"stage": "l2-compile", "target": "域A", "error": "timeout"},
    ])
    assert wiki.failures_summary(out, "l1")["total"] == 1


def test_已解决的失败不计入摘要(tmp_path):
    out = _mk(tmp_path, [{"stage": "l1", "target": "a", "error": "timeout"}])
    FailureLog(out / "failures.jsonl").resolve("l1", "a")
    assert wiki.failures_summary(out)["total"] == 0


# ---------------------------------------------------------------------------
# 查：retry_targets
# ---------------------------------------------------------------------------

def test_重跑目标去重(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l1", "target": "qwen/a", "error": "timeout"},
        {"stage": "l1", "target": "qwen/a", "error": "timeout"},
        {"stage": "l1", "target": "zcode/b", "error": "timeout"},
    ])
    assert wiki.retry_targets(out, "l1") == ["qwen/a", "zcode/b"]


# ---------------------------------------------------------------------------
# 做：retry_failed（不真跑 LLM —— 只验"没事可做"与参数校验两条早返回路径）
# ---------------------------------------------------------------------------

def test_补跑_没有失败项时明确返回(tmp_path):
    out = _mk(tmp_path, [])
    r = wiki.retry_failed(stage="l1", out_dir=out)
    assert r["retried"] == 0
    assert "没有待重跑" in r["message"]


def test_补跑_第二级缺src时明确拒绝(tmp_path):
    out = _mk(tmp_path, [
        {"stage": "l2-compile", "target": "域A", "error": "timeout"},
    ])
    r = wiki.retry_failed(stage="l2", out_dir=out, src="")
    assert r["retried"] == 0
    assert "src" in r["message"]


def test_补跑_第二级没有失败项时明确返回(tmp_path):
    out = _mk(tmp_path, [])
    r = wiki.retry_failed(stage="l2", out_dir=out, src=str(tmp_path))
    assert r["retried"] == 0
    assert "没有待重跑" in r["message"]


def test_补跑全部_没有失败项时给出说明(tmp_path):
    out = _mk(tmp_path, [])
    r = wiki.retry_all(out_dir=out, src=str(tmp_path))
    assert "没有待重跑的失败项" in r.get("message", "")


def test_阶段标签覆盖脚本实际写入的三种(tmp_path):
    """标签必须与脚本写的 stage 名一一对应，否则摘要里会漏项。"""
    assert set(wiki.STAGES) == {"l1", "l2-plan", "l2-compile"}


# ---------------------------------------------------------------------------
# 对齐审计：align（全程只读；重点测引用反推与 manifest diff 两条路径）
# ---------------------------------------------------------------------------

import json as _json
import sqlite3 as _sqlite3

from agentmemhub import wiki_manifest as _wm


def _mkdb(tmp_path, rows):
    db = tmp_path / "session_rag.db"
    conn = _sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE distilled_memories ("
        " id INTEGER PRIMARY KEY, source TEXT, conversation_id TEXT,"
        " type TEXT, confidence TEXT, topic TEXT, status TEXT, content TEXT,"
        " content_hash TEXT, created_at INTEGER)")
    # wiki_compile.list_sessions 会 LEFT JOIN units 投影表取会话标题
    conn.execute("CREATE TABLE units (src_id TEXT, title TEXT)")
    for i, (mid, src, cid, st, h) in enumerate(rows):
        conn.execute("INSERT INTO distilled_memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (mid, src, cid, "concept", "high", "t", st,
                      "内容%d" % mid, h, 1000 + i))
    conn.commit()
    conn.close()
    return db


def _page(out, rel, mids):
    """造一个带 [m<id>] 引用的 md 页面。"""
    f = out / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("测试页\n" + " ".join("[m%d]" % m for m in mids), encoding="utf-8")


def test_对齐_空目录给出明确空结果(tmp_path):
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    r = wiki.align(tmp_path / "out", db=str(db))
    assert r["refs"]["ids"] == 0 and r["refs"]["files"] == 0
    assert r["stages"]["l1"]["manifest"] is False
    assert r["coverage"]["covered"] == 0
    _json.dumps(r, ensure_ascii=False)


def test_对齐_引用反推_死引用与状态漂移(tmp_path):
    db = _mkdb(tmp_path, [
        (1, "a", "s1", "new", "h1"),
        (2, "a", "s1", "merged", "h2"),    # 引用了但已不是输入
        # m3 根本不存在 → missing
    ])
    out = tmp_path / "out"
    _page(out, "p1.md", [1, 2, 3])
    r = wiki.align(out, db=str(db))
    assert r["invalid_refs"]["missing"] == [3]
    assert r["invalid_refs"]["not_input"] == [2]
    assert r["coverage"]["covered"] == 1
    assert r["coverage"]["ratio"] == 1.0   # 分母=当前输入（仅 m1）；引用集里另外两个是失效引用


def test_对齐_引用反推_覆盖率口径只算输入(tmp_path):
    db = _mkdb(tmp_path, [
        (1, "a", "s1", "new", "h1"),
        (2, "a", "s1", "new", "h2"),
        (3, "a", "s1", "merged", "h3"),    # 非输入：不进覆盖率分母
    ])
    out = tmp_path / "out"
    _page(out, "p1.md", [1])
    r = wiki.align(out, db=str(db))
    assert r["coverage"]["current_inputs"] == 2
    assert r["coverage"]["ratio"] == 0.5
    assert r["invalid_refs"]["not_input"] == []   # m3 没被引用，无关


def test_对齐_带manifest检测新增并要求重编(tmp_path):
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    out = tmp_path / "out"
    out.mkdir()
    _wm.write_manifest(_wm.manifest_path(out, "l1"), _wm.build_manifest("l1", db))
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES (2,'a','s1','concept','high','t','new','新增内容','h2',2000)")
    conn.commit()
    conn.close()
    r = wiki.align(out, stage="l1", db=str(db))
    s = r["stages"]["l1"]
    assert s["manifest"] is True
    assert s["added_total"] == 1 and 2 in s["added"]
    assert s["needs_recompile"] is True and r["needs_recompile"] is True
    assert "需要重编译" in r["message"]


def test_对齐_库未变时不需要重编(tmp_path):
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    out = tmp_path / "out"
    out.mkdir()
    _wm.write_manifest(_wm.manifest_path(out, "l2"), _wm.build_manifest("l2", db))
    r = wiki.align(out, stage="l2", db=str(db))
    assert r["needs_recompile"] is False
    assert "库未变化" in r["message"]


def test_对齐_结果整体可JSON序列化(tmp_path):
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    out = tmp_path / "out"
    _page(out, "sub/p1.md", [1])
    r = wiki.align(out, db=str(db))
    _json.dumps(r, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 增量更新：update（mock 掉 LLM —— 测的是编排与连锁，不是模型）
# ---------------------------------------------------------------------------

def _mk_wiki(tmp_path):
    """造一套最小但结构完整的两级产物 + manifest。

    库：m1(new,a/s1) m2(new,a/s1) m3(new,b/s2)
    L1：a__s1.md（1 页，引 m1/m2）、b__s2.md（1 页，引 m3）
    L2：域A（聚合 a__s1.md）、域B（聚合 b__s2.md）
    """
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1"),
                          (2, "a", "s1", "new", "h2"),
                          (3, "b", "s2", "new", "h3")])
    l1 = tmp_path / "out_l1"
    l2 = tmp_path / "out_l2"
    l1.mkdir()

    def l1_file(stem, src, cid, title, summary, mids):
        refs = " ".join("[m%d]" % m for m in mids)
        nums = ",".join(str(i) for i in range(1, len(mids) + 1))
        (l1 / (stem + ".md")).write_text(
            "---\ntitle: %s / %s\nsource: %s\nconversation_id: %s\n"
            "compiled_at: 2026-09-19 10:00:00\nmemories_in: %d\npages_out: 1\n"
            "generator: wiki_compile prototype\n---\n\n# %s\n\n*type: concept*\n\n"
            "正文 %s\n\n**来源**：[%s]\n"
            % (src, cid, src, cid, len(mids), title, refs, nums), encoding="utf-8")
        (l1 / (stem + ".json")).write_text(_json.dumps(
            {"pages": [{"title": title, "type": "concept", "summary": summary,
                        "sources": list(range(1, len(mids) + 1)),
                        "body": "正文 %s" % refs, "related": []}]},
            ensure_ascii=False), encoding="utf-8")

    l1_file("a__s1", "a", "s1", "页A", "摘A", [1, 2])
    l1_file("b__s2", "b", "s2", "页B", "摘B", [3])

    def l2_page(dirname, fname, title, domain, body, src_files):
        d = l2 / dirname
        d.mkdir(parents=True, exist_ok=True)
        (d / (fname + ".md")).write_text(
            "---\ntitle: %s\ndomain: %s\ntype: concept\n"
            "compiled_at: 2026-09-19 11:00:00\nsources: 2\nmerged_from: 1\n"
            "generator: wiki_aggregate prototype\n---\n\n# %s\n\n%s\n"
            % (title, domain, title, body), encoding="utf-8")
        (d / (fname + ".json")).write_text(_json.dumps(
            {"title": title, "type": "concept", "summary": "摘-" + title,
             "body": body, "sources": [], "from_files": src_files,
             "from_titles": [title]}, ensure_ascii=False), encoding="utf-8")
        (d / "index.md").write_text(
            "# %s\n\n共 1 页。\n- [%s](%s.md) — 摘-%s\n"
            % (domain, title, fname, title), encoding="utf-8")

    l2_page("01-域A", "001-页A", "页A", "域A", "聚合正文 [m1][m2]", ["a__s1.md"])
    l2_page("02-域B", "001-页B", "页B", "域B", "聚合正文 [m3]", ["b__s2.md"])
    (l2 / "_domains.json").write_text(_json.dumps(
        [{"name": "域A", "why": "", "members": [1]},
         {"name": "域B", "why": "", "members": [2]}], ensure_ascii=False), encoding="utf-8")

    from agentmemhub import wiki_manifest as _w
    m1 = _w.build_manifest("l1", db)
    _w.write_manifest(_w.manifest_path(l1, "l1"), m1)
    m2 = _w.build_manifest("l2", db, extra={
        "src_dir": str(l1), "n_pages": 2,
        "domains": {"域A": {"dir": "01-域A", "pages": 1, "l1_files": ["a__s1.md"],
                            "mids": [1, 2]},
                    "域B": {"dir": "02-域B", "pages": 1, "l1_files": ["b__s2.md"],
                            "mids": [3]}}})
    _w.write_manifest(_w.manifest_path(l2, "l2"), m2)
    return db, l1, l2


class _FakeClient:
    """按 system/user 特征分流的假 LLM：assign / 域内细分 / 最终页编译。

    SYSTEM_PLAN 与 SYSTEM_COMPILE 同以「你是知识库编辑」开头，只能靠 user
    里要求返回的字段名区分（assign / members / body）。
    """

    def complete_json(self, system, user, max_tokens=0, temperature=0.0):
        if "知识库架构师" in system:
            n = len([x for x in user.splitlines() if x.startswith("[")])
            return {"assign": {str(i): 1 for i in range(1, n + 1)}}
        if '"body": "markdown 正文"' in user:      # 最终页编译
            return {"title": "页A新", "type": "concept", "summary": "新摘要",
                    "body": "重编正文", "related": []}
        return {"pages": [{"title": "新合并页", "members": [1], "why": "w"}]}


def test_update_缺manifest时明确拒绝(tmp_path):
    _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    r = wiki.update(l1_dir=tmp_path / "x", l2_dir=tmp_path / "y",
                    db=str(tmp_path / "session_rag.db"))
    assert r["updated"] is False
    assert "manifest" in r["message"]


def test_update_库未变时无需更新(tmp_path):
    db, l1, l2 = _mk_wiki(tmp_path)
    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r["updated"] is False and "库未变化" in r["message"]


def test_update_新增记忆触发L1重编与脏域重编(tmp_path, monkeypatch):
    db, l1, l2 = _mk_wiki(tmp_path)
    # RAG 侧：会话 a/s1 新增一条记忆 → 只有域A 该动
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES (4,'a','s1','concept','high','t','new','新增内容','h4',2000)")
    conn.commit(); conn.close()

    wiki._ensure_scripts_on_path()      # 与服务层同一机制，scripts 才可 import
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)
    monkeypatch.setattr(_wc, "compile_session",
                        lambda client, memories, log=print: {
                            "session_summary": "新概要",
                            "pages": [{"title": "页A", "type": "concept",
                                       "summary": "新摘A",
                                       "sources": [1, 2, 3],
                                       "body": "更新后正文", "related": []}]})
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r["updated"] is True
    assert r["dirty_domains"] == ["域A"]                     # 域B 不受影响
    assert r["l1"]["recompiled"] == 1 and r["l1"]["failed"] == 0

    # L1 已重写
    assert "更新后正文" in (l1 / "a__s1.md").read_text(encoding="utf-8")
    # L2 域A 已随新 L1 重写（单成员页沿用源页内容 —— 省一次 LLM 调用的设计）；
    # 域B 原样
    page_a = (l2 / "01-域A" / "001-页A.md").read_text(encoding="utf-8")
    assert "更新后正文" in page_a and "[m4]" in page_a      # 新记忆已进聚合页
    assert (l2 / "02-域B" / "001-页B.md").exists()
    assert "聚合正文 [m3]" in (l2 / "02-域B" / "001-页B.md").read_text(encoding="utf-8")

    # manifest 已刷新 → 再审计应归零
    a = wiki.align(l1, "l1", db=str(db))
    assert a["needs_recompile"] is False
    a2 = wiki.align(l2, "l2", db=str(db))
    assert a2["needs_recompile"] is False


def test_update_全新会话走L1编译并归入现有域(tmp_path, monkeypatch):
    """用户点名的特殊场景：全新会话的 RAG 记忆，现有 wiki 完全没有 ——
    L1 从零编译出全新页，再归入现有域，所在域跟着重编。"""
    db, l1, l2 = _mk_wiki(tmp_path)
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES (5,'c','s3','concept','high','t','new','全新会话内容','h5',3000)")
    conn.commit(); conn.close()

    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)
    monkeypatch.setattr(_wc, "compile_session",
                        lambda client, memories, log=print: {
                            "session_summary": "全新概要",
                            "pages": [{"title": "页C", "type": "concept",
                                       "summary": "摘C", "sources": [1],
                                       "body": "全新正文", "related": []}]})
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r["updated"] is True

    # L1：全新产物落盘
    assert (l1 / "c__s3.md").exists()
    assert "全新正文" in (l1 / "c__s3.md").read_text(encoding="utf-8")
    # 新文件被归入域A（FakeClient 的 assign 全归 1 号域）→ 域A 重编
    assert r["dirty_domains"] == ["域A"]
    # 域A 目录里：新会话页已入域；旧页已按重编结果重建（无幽灵残留）
    assert (l2 / "01-域A" / "index.md").exists()
    names = sorted(f.name for f in (l2 / "01-域A").glob("*.md") if f.name != "index.md")
    assert all("页A" in n or "页C" in n for n in names)   # 没有别的幽灵页
    # 域B 不受影响
    assert "聚合正文 [m3]" in (l2 / "02-域B" / "001-页B.md").read_text(encoding="utf-8")
    # manifest 刷新后归零，且域映射里能找到新文件
    from agentmemhub import wiki_manifest as _w
    mf = _w.load_manifest(_w.manifest_path(l2, "l2"))
    assert "c__s3.md" in mf["domains"]["域A"]["l1_files"]
    assert wiki.align(l1, "l1", db=str(db))["needs_recompile"] is False


def test_归类视野_代表页标题进提示词(tmp_path):
    """增量归类的 LLM 只看域名+一句话 why 会归错域 —— 必须能看到每域的
    现有页标题长什么样（视野与全量归类对齐）。"""
    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa

    seen = {}

    class _Cap:
        def complete_json(self, system, user, max_tokens=0, temperature=0.0):
            seen["user"] = user
            return {"assign": {"1": 1}}

    page = {"source": "c", "cid": "s9", "title": "神秘新页", "body": "",
            "mids": [], "file": "c__s9.md"}
    r = _wa.assign_new_pages(
        _Cap(), [{"name": "域A", "why": "w"}], [page],
        rep_pages={"域A": ["ADBController 核心设计", "ROM 兼容适配"]})
    assert r == {"域A": [page]}
    assert "ADBController 核心设计" in seen["user"]      # 代表页可见
    assert "神秘新页" in seen["user"]                    # 新页待归


def test_update_会话输入清空时删除其L1产物(tmp_path, monkeypatch):
    db, l1, l2 = _mk_wiki(tmp_path)
    conn = _sqlite3.connect(db)
    conn.execute("UPDATE distilled_memories SET status='duplicate' WHERE id IN (1,2)")
    conn.commit(); conn.close()

    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)
    monkeypatch.setattr(_wc, "compile_session",
                        lambda client, memories, log=print: {"pages": []})
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r["updated"] is True
    assert not (l1 / "a__s1.md").exists()
    assert not (l1 / "a__s1.json").exists()


# ---------------------------------------------------------------------------
# L1 失败的静默丢失（2026-09-21 线上实测复现）
# ---------------------------------------------------------------------------

def _fail_session(monkeypatch, msg="LLMTransientError: TimeoutError: read timed out"):
    """让 L1 编译整体失败（模拟分组调用连续读超时）。"""
    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)

    def _boom(client, memories, log=print):
        raise RuntimeError(msg)

    monkeypatch.setattr(_wc, "compile_session", _boom)
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())


def _add_memory(db, mid, src, cid, content):
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES "
                 "(%d,'%s','%s','concept','high','t','new','%s','h%d',2000)"
                 % (mid, src, cid, content, mid))
    conn.commit(); conn.close()


def test_update_L1失败时不把失败会话写进新基线(tmp_path, monkeypatch):
    """复现当天真实的静默丢失。

    L1 编译失败后，update 仍用「库当前状态」重建 manifest 并落盘，于是失败
    会话被**谎报为已编译** —— 此后 align 一律报"库未变化"，这批记忆再也
    不会重编。manifest 必须只记**实际编译成功**的输入（它是重试的唯一线索）。
    """
    db, l1, l2 = _mk_wiki(tmp_path)
    _add_memory(db, 4, "a", "s1", "新增内容")
    _fail_session(monkeypatch)

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r["l1"]["failed"] == 1

    a = wiki.align(l1, "l1", db=str(db))
    assert a["needs_recompile"] is True, "失败会话被写进新基线了 —— 脏数据被永久掩盖"
    assert a["stages"]["l1"]["added_total"] >= 1
    assert "a/s1" in (a["stages"]["l1"]["dirty_sessions"] or {})
    # 失败不该破坏已有产出
    assert (l1 / "a__s1.md").exists()


def test_update_L1全失败时跳过L2避免无效重写(tmp_path, monkeypatch):
    """L1 一条都没编出来时，L2 只会拿**磁盘上的旧 L1** 重跑一遍同样的域 ——
    实测白烧 1290 秒 / 42 万 prompt token。全失败就跳过，等重试。"""
    db, l1, l2 = _mk_wiki(tmp_path)
    _add_memory(db, 4, "a", "s1", "新增内容")
    _fail_session(monkeypatch)

    before = (l2 / "01-域A" / "001-页A.md").read_text(encoding="utf-8")
    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))

    assert r["l2"]["domains_recompiled"] == 0
    assert r["l2"].get("skipped")
    after = (l2 / "01-域A" / "001-页A.md").read_text(encoding="utf-8")
    assert after == before, "L2 不该基于未更新的 L1 重写"


def test_对齐_未被页面覆盖的输入要浮出来(tmp_path):
    """align 只比 manifest diff，从不看"页面实际覆盖了多少输入"。

    实测库里 1781 条输入有 **61 条无任何页面引用**（最早可追到 09-10），
    而 align 一直报"✓ 库未变化" —— 静默缺口必须有出口，否则没人会发现。
    """
    db, l1, l2 = _mk_wiki(tmp_path)
    _add_memory(db, 4, "a", "s1", "从未被任何页面引用")

    # manifest 里"已编译"（含 m4），但页面确实没引用它 —— 制造静默缺口
    from agentmemhub import wiki_manifest as _w
    _w.write_manifest(_w.manifest_path(l1, "l1"), _w.build_manifest("l1", db))

    a = wiki.align(l1, "l1", db=str(db))
    assert a["needs_recompile"] is False          # diff 视角：确实没变化
    assert a["coverage"]["uncovered"] == 1        # 但覆盖视角：有缺口
    assert "未覆盖" in a["message"]


def test_update_组级失败的记忆不写进新基线(tmp_path, monkeypatch):
    """逐组编译是 fail-open 的：单组失败不影响整会话，所以会话整体算"成功"。

    但失败那组的记忆**没进任何页面** —— 必须照样剔除出新基线，否则下次 align
    会以为已编译，它们就被静默丢弃了（与"会话级失败"同一个坑，粒度更细；
    2026-09-21 切网时实测丢了几组）。
    """
    db, l1, l2 = _mk_wiki(tmp_path)
    _add_memory(db, 4, "a", "s1", "新增内容")

    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)
    # 会话整体成功（没抛异常），但第 2 条记忆所在的分组编译失败了
    monkeypatch.setattr(_wc, "compile_session",
                        lambda client, memories, log=print: {
                            "session_summary": "概要",
                            "pages": [{"title": "页A", "type": "concept",
                                       "summary": "s", "sources": [1, 3],
                                       "body": "正文 [m1][m3]", "related": []}],
                            "failed_members": [2]})
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r["l1"]["failed"] == 0             # 会话级没失败
    assert r["l1"]["failed_ids"] == 1         # 但组级失败了 1 条

    # 失败那条不进新基线 → 下次 align 仍报脏、会重试
    a = wiki.align(l1, "l1", db=str(db))
    assert a["needs_recompile"] is True
    assert a["stages"]["l1"]["added_total"] == 1
    assert any("分组" in h for h in r["hints"])

