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
# L2 新基线必须剔除「没真正写出来」的域
# （2026-09-25：聚合中止/跳过不再掩盖脏数据）
# ---------------------------------------------------------------------------

def test_L2基线剔除未写出的脏域():
    """**关键回归**：没编译却记成"已编译" = 永久掩盖（align 下轮不再报脏）。

    实测事故：L2 聚合被防护**中止**（产物一个字节没动），但旧代码仍刷新了 L2
    manifest → `agentmemhub wiki --action pending` 报 0 条，而 L2 产物比 L1
    整整落后近 4 小时。三类"没写出来"的域都必须在基线里剔除。
    """
    dom_out = {"域A": {"mids": [1, 2]}, "域B": {"mids": [3]},
               "域C": {"mids": [4]}, "域D": {"mids": [5]}}

    # 域A 写了；域B 被「页数崩塌」跳过；域C 是脏域但聚合压根没写；域D 不脏也没写
    assert wiki._l2_skip_ids(dom_out, ["域A", "域B", "域C"],
                             {"域A"}, ["域B"]) == {3, 4}

    # 正常路径：全部写出 → 不剔除
    assert wiki._l2_skip_ids(dom_out, ["域A"], {"域A"}, []) == set()

    # 没有脏域（本就没东西要重编）→ 不剔除
    assert wiki._l2_skip_ids(dom_out, [], set(), []) == set()

    # 聚合"什么都没写"（如只归域的 dry-run、异常提前返回）→ 脏域全部剔除
    assert wiki._l2_skip_ids(dom_out, ["域A", "域B"], set(), []) == {1, 2, 3}

    # 不脏但被跳过的域同样要剔除 —— 它确实没写出来
    assert wiki._l2_skip_ids(dom_out, [], {"域A"}, ["域D"]) == {5}


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


def _l2_page(out, dirname, stem, mids, files=()):
    """造一个 L2 域页面 —— **json 才 `_rebuild_l2_manifest` 读的那份**。"""
    d = out / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.md").write_text("# %s\n" % dirname.split("-", 1)[-1], encoding="utf-8")
    (d / ("%s.md" % stem)).write_text(
        "页\n" + " ".join("[m%d]" % m for m in mids), encoding="utf-8")
    (d / ("%s.json" % stem)).write_text(_json.dumps({
        "title": stem, "body": "正文 " + " ".join("[m%d]" % m for m in mids),
        "sources": ["m%d" % m for m in mids],
        "from_files": list(files)}, ensure_ascii=False), encoding="utf-8")


def _l2_with_domain(tmp_path, members=(1,)):
    out = tmp_path / "l2"
    out.mkdir(parents=True, exist_ok=True)
    (out / "_domains.json").write_text(
        _json.dumps([{"name": "域A", "members": list(members)}], ensure_ascii=False),
        encoding="utf-8")
    return out


def test_落盘轮_批次提交后新增的记忆不进基线(tmp_path):
    """**离线异步的真实风险**（2026-10-03 用户提出）：

    "提交时的快照"与"落盘时的库"之间隔了几小时甚至一天，期间新写入的记忆**没有被
    这次编译覆盖**。若照"当前库"把它记进基线，它会被**静默掩盖** —— 下次增量更新
    不再带上它，这条记忆**永远进不了 wiki**。实时通道没有这个问题（提交即落盘，
    中间没有时间窗）。
    """
    # created_at：mid=1 → 1000（批次之前）；mid=2 → 1001（批次之后）
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1"), (2, "a", "s1", "new", "h2")])
    out = _l2_with_domain(tmp_path)
    _l2_page(out, "01-域A", "001-页", [1], files=["a__s1.md"])
    wiki._ensure_scripts_on_path()          # `_rebuild_l2_manifest` 要 import wiki_aggregate

    mf = wiki._rebuild_l2_manifest(out, str(db), since=1000.5)

    assert "1" in mf["inputs"], "批次前就存在、且被页面引用的输入要留在基线里"
    assert "2" not in mf["inputs"], \
        "**批次提交后新增**的输入不能被记成已编译（否则它永远进不了 wiki）"
    assert mf["dropped_new_inputs"] == 1


def test_落盘轮_不传since时保留全部(tmp_path):
    """兼容旧行为：不传 `since` 就不做剔除（历史调用点/测试依赖这个语义）。"""
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1"), (2, "a", "s1", "new", "h2")])
    out = _l2_with_domain(tmp_path)
    _l2_page(out, "01-域A", "001-页", [1])
    wiki._ensure_scripts_on_path()

    mf = wiki._rebuild_l2_manifest(out, str(db))

    assert "1" in mf["inputs"] and "2" in mf["inputs"]


def test_落盘轮_已被引用的输入即使晚于since也保留(tmp_path):
    """双保险：`drop_ids` 会减去"磁盘产物确实引用"的集合 —— 已编译的绝不能被剔除，
    否则基线漏掉它，下一轮会把它当"没编译过"重复提交。"""
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])       # created_at = 1000
    out = _l2_with_domain(tmp_path)
    _l2_page(out, "01-域A", "001-页", [1])
    wiki._ensure_scripts_on_path()

    mf = wiki._rebuild_l2_manifest(out, str(db), since=1.0)   # since 远早于它

    assert "1" in mf["inputs"], "被页面引用过的输入必须留下"


def test_全量模式不被缺manifest挡住(tmp_path, monkeypatch):
    """**全量重跑正是"建立锚点"的手段**，所以不该被"缺少编译清单"那道早退挡住 ——
    否则第一次全量永远跑不起来（自相矛盾）。同时验证它确实**重新归域**：
    `partial=False`（覆盖写盘、清多余目录）+ `no_resume=True`（忽略既有域结构）
    + 不预设任何域。
    """
    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    l1, l2 = tmp_path / "l1", tmp_path / "l2"
    l1.mkdir()
    l2.mkdir()                              # 两个目录里都**没有** manifest

    # 先确认既有行为还在：增量模式被"缺 manifest"早退
    r_inc = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))
    assert r_inc.get("updated") is False
    assert "缺少编译清单" in (r_inc.get("message") or "")

    wiki._ensure_scripts_on_path()
    seen: dict = {}

    def _fake_run(args):
        seen["called"] = True
        seen["partial"] = args.partial
        seen["no_resume"] = args.no_resume
        seen["preset"] = list(args.preset_domains or [])
        return {"aborted": "测试中止（不继续跑收尾）", "written_domains": [],
                "skipped_domains": [], "pages": 0, "pages_by_domain": {}}

    import wiki_aggregate as _wa
    monkeypatch.setattr(_wa, "run", _fake_run)
    # 快照与投影是旁路且很慢，测试里挡掉（它们的行为另有测试覆盖）
    from agentmemhub import snapshot as _snap
    monkeypatch.setattr(_snap, "create", lambda **k: {})

    wiki.update(l1_dir=l1, l2_dir=l2, db=str(db), full=True)

    assert seen.get("called"), "全量模式**不该**被'缺 manifest'早退挡住"
    assert seen["partial"] is False, "全量要覆盖写盘（清掉多余域目录）"
    assert seen["no_resume"] is True, "全量不沿用既有 _domains.json 的域结构"
    assert seen["preset"] == [], "全量不预设域 —— 交给聚合层重新归域"


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


def _offline_job(**kw):
    from agentmemhub.llm_batch import BatchJob
    base = dict(batch_id="batch-x", stage="l2plan", submitted_at=1000.0)
    base.update(kw)
    return BatchJob(**base)


class _OfflineClient:
    """假批量客户端（不发任何请求）。"""

    def __init__(self, *, status="completed", files=None):
        self.status = status
        self.files = files or {}

    def retrieve(self, batch_id):
        return {"status": self.status}

    def download(self, file_id):
        return self.files.get(file_id, "")


def _member_snap(title="新页", body="正文 [m1]", fname="a__s1.md"):
    return {"title": title, "type": "concept", "summary": "摘要",
            "body": body, "mids": [1], "file": fname}


def test_离线模式_编译结果到位就落盘并刷基线(tmp_path, monkeypatch):
    """M2 收尾：有在飞任务时 update() 先回读；编译结果到位 → 落盘 + 刷基线 + 消费任务。"""
    db, l1, l2 = _mk_wiki(tmp_path)
    from agentmemhub.llm_batch import STATE_NAME, load_state, save_state

    save_state(l2 / STATE_NAME, [_offline_job(
        batch_id="batch-page", stage="l2compile", provider_status="completed",
        items={"l2page|域A-x|1": {"domain": "域A", "page_no": 1}},
        members={"域A": [{"ti": 1, "title": "新页", "needs_llm": False,
                          "dropped": 0, "group": [_member_snap()]}]})])
    monkeypatch.setattr(wiki, "_verify_links",
                        lambda p: {"available": False, "ok": True})

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db),
                    offline_client=_OfflineClient())

    assert r["step"] == "landed" and r["updated"] is True
    assert list(l2.glob("0*-*/001-新页.md")), "页面要写进域目录"
    assert load_state(l2 / STATE_NAME) == [], "落盘后任务被消费，不再留在 state"
    assert r["manifest"]["l2_inputs"] >= 1, "基线必须刷新，否则下轮会重复提交"


def test_离线模式_零请求时直接落盘而不是空等(tmp_path, monkeypatch):
    """全是单成员页时编译段**没有请求可交** → 必须直接落盘。

    否则下次调用会因为没有在飞任务而重新走提交轮，永远到不了落盘。
    """
    db, l1, l2 = _mk_wiki(tmp_path)
    from agentmemhub.llm_batch import STATE_NAME, load_state, save_state

    save_state(l2 / STATE_NAME, [_offline_job(
        batch_id="batch-plan", stage="l2plan", provider_status="completed",
        items={"l2plan|域A-x|0": {"domain": "域A", "batch_no": 0}},
        members={"域A": [_member_snap()]},
        results={"l2plan|域A-x|0": {"ok": True, "data": {
            "pages": [{"title": "新页", "members": [1]}]}}})])
    monkeypatch.setattr(wiki, "_verify_links",
                        lambda p: {"available": False, "ok": True})

    class _Runner:
        submissions: list = []
        on_submitted = None
        stage = ""

        def run(self, reqs):
            raise AssertionError("全是单成员页，不该提交任何请求")

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db),
                    offline_client=_OfflineClient(), offline_runner=_Runner())

    assert r["step"] == "landed", "零请求时应当直接落盘"
    assert list(l2.glob("0*-*/001-新页.md"))
    assert load_state(l2 / STATE_NAME) == []


def test_离线模式_还在等就如实报告进度(tmp_path):
    db, l1, l2 = _mk_wiki(tmp_path)
    from agentmemhub.llm_batch import STATE_NAME, save_state

    save_state(l2 / STATE_NAME, [_offline_job(
        batch_id="batch-wait", stage="l2plan", submitted_at=1000.0,
        items={"a": {"domain": "域A", "batch_no": 0},
               "b": {"domain": "域A", "batch_no": 1}})])

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db),
                    offline_client=_OfflineClient(status="in_progress"))

    assert r["step"] == "waiting" and r["updated"] is False
    w = r["waiting"][0]
    assert w["batch_id"] == "batch-wait" and w["status"] == "in_progress"
    assert w["got"] == 0 and w["total"] == 2
    assert any("仍在等厂商" in h for h in r["hints"])


def test_离线模式_没有在飞任务时走提交轮(tmp_path, monkeypatch):
    """没有在飞任务 → `_offline_advance` 返回 None → 落到正常流程，并把运行器
    透传给聚合层（由它走"整体一次提交"）。"""
    db, l1, l2 = _mk_wiki(tmp_path)
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES "
                 "(5,'c','s3','concept','high','t','new','离线提交正文','h5',3000)")
    conn.commit()
    conn.close()

    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)
    monkeypatch.setattr(_wc, "compile_session",
                        lambda client, memories, log=print: {
                            "session_summary": "概要",
                            "pages": [{"title": "页A", "type": "concept",
                                       "summary": "摘A", "sources": [1],
                                       "body": "正文", "related": []}]})
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())

    seen = {}

    def _fake_run(args):
        seen["offline_runner"] = getattr(args, "offline_runner", "缺字段")
        seen["offline_compile_runner"] = getattr(args, "offline_compile_runner",
                                                 "缺字段")
        return {"aborted": "", "written_domains": [], "skipped_domains": [],
                "pages": 0, "pages_by_domain": {},
                "offline_submitted": {"batch_id": "batch-new", "stage": "l2plan",
                                      "n_requests": 2, "n_domains": 1,
                                      "domains": ["域A"], "pending_ids": ["x"]}}

    monkeypatch.setattr(_wa, "run", _fake_run)
    marker = object()

    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db),
                    offline_client=_OfflineClient(), offline_runner=marker)

    assert seen["offline_runner"] is None, "默认**细分走实时**（只离线编译）"
    assert seen["offline_compile_runner"] is marker, "运行器要透传到编译段"
    assert r["l2"]["offline_submitted"]["batch_id"] == "batch-new"
    assert r["l2"]["offline_submitted"]["stage"] == "l2plan"


def test_update_离线提交轮不刷L2基线也不跑linkfix(tmp_path, monkeypatch):
    """M2b-1：离线提交轮**不是失败**，但同样绝不能刷 L2 基线 —— 刷了那批脏数据
    就被当成"已编译"，回读轮回来也没得编（与中止闸同一个理由）。"""
    db, l1, l2 = _mk_wiki(tmp_path)
    # 必须先制造脏数据：没有待编译内容时 update() 会走"库未变化"分支提前返回
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO distilled_memories VALUES "
                 "(5,'c','s3','concept','high','t','new','离线提交测试正文','h5',3000)")
    conn.commit()
    conn.close()

    wiki._ensure_scripts_on_path()
    import wiki_aggregate as _wa
    import wiki_compile as _wc
    monkeypatch.setattr(_wc, "_make_client", lambda thinking="": None)
    monkeypatch.setattr(_wc, "compile_session",
                        lambda client, memories, log=print: {
                            "session_summary": "概要",
                            "pages": [{"title": "页A", "type": "concept",
                                       "summary": "摘A", "sources": [1],
                                       "body": "正文", "related": []}]})
    monkeypatch.setattr(_wa, "make_client", lambda thinking="": _FakeClient())
    # 让聚合层"把活交出去了"：如实回报 offline_submitted（不是 aborted）
    monkeypatch.setattr(_wa, "run", lambda args: {
        "aborted": "", "written_domains": [], "skipped_domains": [], "pages": 0,
        "pages_by_domain": {},
        "offline_submitted": {"batch_id": "batch-x", "n_requests": 1, "n_domains": 1,
                              "domains": ["域A"], "pending_ids": ["l2plan|域A-abc|0"]}})

    mf_before = (l2 / "manifest_l2.json").read_text(encoding="utf-8")
    r = wiki.update(l1_dir=l1, l2_dir=l2, db=str(db))

    assert r["l2"]["offline_submitted"]["batch_id"] == "batch-x"
    assert r["linkfix"] == {"skipped": True}, "提交轮不该跑 linkfix（产物没被重写）"
    assert (l2 / "manifest_l2.json").read_text(encoding="utf-8") == mf_before, \
        "提交轮绝不能刷 L2 基线（内容必须一字不变）：刷了脏数据就被当成已编译"
    assert any("离线批量任务" in h for h in r["hints"]), "要给人一条可读提示"


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


# ---------------------------------------------------------------------------
# 两级一致性（2026-09-25：L2 落后于 L1 这种中间态此前完全查不出来）
#
# 背景：`pending` 只比 L1 的 manifest，所以"L1 已重编、L2 没跟上"它报 0；
# 而 `align` 一次只吃一个目录，两级 manifest 却分居两个目录 —— 于是这个
# 中间态（实测真的出现过：L2 17:11 / L1 21:01，差近 4 小时）谁也看不见。
# ---------------------------------------------------------------------------

def _stage(**kw) -> dict:
    base = {"manifest": True, "generated_at": "2026-09-25T21:32:50",
            "n_inputs": 1800}
    base.update(kw)
    return base


def test_两级同步_同刻判ok():
    c = wiki._consistency({"l1": _stage(), "l2": _stage()})
    assert c["comparable"] is True
    assert c["verdict"] == "ok" and c["skew_seconds"] == 0.0
    assert c["inputs_delta"] == 0


def test_两级同步_L1领先判l2落后():
    """这是**需要重编 L2** 的信号：L1 已经按新输入重编，页面里还是旧知识。"""
    c = wiki._consistency({
        "l1": _stage(generated_at="2026-09-25T21:32:50", n_inputs=1800),
        "l2": _stage(generated_at="2026-09-25T17:11:35", n_inputs=1798)})
    assert c["verdict"] == "l1_ahead"
    assert c["skew_seconds"] > 3600
    assert c["inputs_delta"] == 2


def test_两级同步_L2领先判l1落后():
    c = wiki._consistency({
        "l1": _stage(generated_at="2026-09-25T17:11:35"),
        "l2": _stage(generated_at="2026-09-25T21:32:50")})
    assert c["verdict"] == "l2_ahead"
    assert c["skew_seconds"] < 0


def test_两级同步_容差内不算落后():
    """一次正常的 update 会在几秒内写完两级 —— 别把正常抖动报成异常。"""
    c = wiki._consistency({
        "l1": _stage(generated_at="2026-09-25T21:32:50"),
        "l2": _stage(generated_at="2026-09-25T21:32:20")})
    assert c["verdict"] == "ok"


def test_两级同步_manifest不齐时明确说无法比较():
    c = wiki._consistency({"l1": _stage(), "l2": {"manifest": False}})
    assert c["comparable"] is False
    assert "l2" in c["reason"]


def test_两级同步_时间解析不了也不炸():
    c = wiki._consistency({"l1": _stage(generated_at="坏值"), "l2": _stage()})
    assert c["comparable"] is False
    assert "解析" in c["reason"]


def test_对齐_缺manifest时带上期望路径(tmp_path):
    """**关键回归**：早先只回一个 False，CLI 就编出"编译于该功能上线前"这种
    假原因 —— 而真相是"文件在另一个目录"。带上 path 才说得清。"""
    from agentmemhub import wiki_manifest as wm

    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    l1 = tmp_path / "l1"
    l1.mkdir()
    _page(l1, "p1.md", [1])
    wm.write_manifest(wm.manifest_path(l1, "l1"),
                      wm.build_manifest("l1", str(db)))   # 只有 L1 的清单

    r = wiki.align(l1, db=str(db))              # 单目录用法（不带 stage）→ 查两级

    assert r["stages"]["l1"]["manifest"] is True
    assert r["stages"]["l1"]["path"].endswith("manifest_l1.json")
    assert r["stages"]["l2"]["manifest"] is False
    assert r["stages"]["l2"]["path"].endswith("manifest_l2.json")
    assert r["consistency"]["comparable"] is False


def test_对齐_两级一起对(tmp_path):
    from agentmemhub import wiki_manifest as wm

    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    l1, l2 = tmp_path / "l1", tmp_path / "l2"
    _page(l1, "p1.md", [1])
    _page(l2, "01-域/p1.md", [1])
    for d, st in ((l1, "l1"), (l2, "l2")):
        wm.write_manifest(wm.manifest_path(d, st),
                          wm.build_manifest(st, str(db)))

    r = wiki.align(l1_dir=l1, l2_dir=l2, db=str(db))

    assert set(r["dirs"]) == {"l1", "l2"}
    assert r["stages"]["l1"]["manifest"] and r["stages"]["l2"]["manifest"]
    assert r["consistency"]["comparable"] is True
    assert r["consistency"]["verdict"] == "ok"
    _json.dumps(r, ensure_ascii=False)          # 必须可序列化（CLI/面板共用）


def test_对齐_不给目录时按配置解析(tmp_path, monkeypatch):
    """什么都不给 → 从 `wiki.out_l1` / `wiki.out_l2` 解析两级，而不是报错要求 --out。"""
    from agentmemhub import config as hub_config
    from agentmemhub import wiki_manifest as wm

    db = _mkdb(tmp_path, [(1, "a", "s1", "new", "h1")])
    l1, l2 = tmp_path / "l1", tmp_path / "l2"
    _page(l1, "p1.md", [1])
    _page(l2, "01-域/p1.md", [1])
    for d, st in ((l1, "l1"), (l2, "l2")):
        wm.write_manifest(wm.manifest_path(d, st),
                          wm.build_manifest(st, str(db)))
    monkeypatch.setattr(hub_config, "config",
                        lambda: type("C", (), {"wiki": {"out_l1": str(l1),
                                                        "out_l2": str(l2)}})())

    r = wiki.align(db=str(db))

    assert set(r["dirs"]) == {"l1", "l2"}
    assert r["consistency"]["comparable"] is True


def test_离线推进自己保证scripts在path(tmp_path, monkeypatch):
    """⚠️ **收割路径不经过 `update()`** —— 而 `update()` 才是原本调
    `_ensure_scripts_on_path()` 的地方。

    实测后果（2026-10-04 体检在 `harvest_state.json` 的 `last_error` 里发现）：
    MCP 进程里写记忆触发的收割会 `ModuleNotFoundError: No module named
    'wiki_aggregate'` —— 而收割是**旁路**（不抛），于是**静默失败**：
    用户以为"自动收割生效了"，其实一次都没跑成。

    这条测试守住"**函数自己保证依赖**"，而不是指望每个调用方都记得先补 path。
    """
    import sys
    from pathlib import Path as _P

    scripts = str(_P(wiki.__file__).resolve().parents[1] / "scripts")
    # 模拟 MCP 进程：`scripts/` 不在 path 上，且模块缓存里也没有
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != scripts])
    monkeypatch.delitem(sys.modules, "wiki_aggregate", raising=False)
    assert scripts not in sys.path and "wiki_aggregate" not in sys.modules

    l1, l2 = tmp_path / "l1", tmp_path / "l2"
    l1.mkdir()
    l2.mkdir()

    # 没有在飞任务 ⇒ 提前返回 None（调用方据此走提交轮），但**必须已经 import 成功**
    r = wiki._offline_advance(l1=l1, l2=l2, db="", runner=None, client=None,
                              deadline_s=0.0)

    assert r is None, "没有在飞任务时应返回 None"
    assert "wiki_aggregate" in sys.modules, \
        "`_offline_advance()` 必须自己把 scripts 加进 path（不能依赖调用方）"
    assert scripts in sys.path

