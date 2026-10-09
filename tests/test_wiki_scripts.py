# -*- coding: utf-8 -*-
"""LLM Wiki 生成流程的可复现性测试。

覆盖的是**纯函数与产物解析**——不调 LLM、不碰真数据库、不需要任何凭据，
所以任何人 clone 下来就能跑，用来确认"这套 wiki 生成流程在我的环境里可行"。
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# 文件名 slug
# ---------------------------------------------------------------------------

def test_slug_去掉中英文标点():
    from wiki_aggregate import _slug
    assert _slug("A：B（C）/D") == "A-B-C-D"
    assert _slug("a, b. c、d") == "a-b-c-d"


def test_slug_不会截断在括号中间():
    """实测踩过：按字符硬截会产出 `...settings.json（.md` 这种断名。"""
    from wiki_aggregate import _slug
    s = _slug("Qwen Code 接入第三方/自定义模型：settings.json（openai provider）配置说明")
    assert "（" not in s and "）" not in s and "：" not in s
    assert 0 < len(s) <= 40


def test_slug_空标题有兜底():
    from wiki_aggregate import _slug
    assert _slug("") == "page"
    assert _slug("///") == "page"


# ---------------------------------------------------------------------------
# 第一级产物的解析
# ---------------------------------------------------------------------------

_MD = "\n".join([
    "---",
    "title: src / cid",
    "source: src",
    "conversation_id: cid",
    "memories_in: 3",
    "pages_out: 2",
    "---",
    "",
    "> 会话简介",
    "",
    "# 第一页标题",
    "",
    "*type: concept*",
    "",
    "**摘要**：第一页摘要。",
    "",
    "正文引用 [1] 和 [3]。",
    "",
    "```bash",
    "# 这是代码块里的注释，不能被当成页面边界",
    "echo hi",
    "```",
    "",
    "**相关**：[[第二页标题]] · [[不存在的页]]",
    "",
    "**来源**：[m101] [m103]",
    "",
    "---",
    "",
    "# 第二页标题",
    "",
    "*type: entity*",
    "",
    "**摘要**：第二页摘要。",
    "",
    "正文引用 [2]。",
    "",
    "**来源**：[m102]",
    "",
    "---",
    "",
])

_JSON = {"pages": [
    {"title": "第一页标题", "type": "concept", "summary": "第一页摘要。",
     "sources": [1, 3], "body": "正文引用 [1] 和 [3]。",
     "related": ["第二页标题", "不存在的页"]},
    {"title": "第二页标题", "type": "entity", "summary": "第二页摘要。",
     "sources": [2], "body": "正文引用 [2]。", "related": []},
]}


def _fake_l1(tmp_path: Path) -> Path:
    d = tmp_path / "l1"
    d.mkdir()
    (d / "src__cid.md").write_text(_MD, encoding="utf-8")
    (d / "src__cid.json").write_text(json.dumps(_JSON, ensure_ascii=False),
                                     encoding="utf-8")
    return d


def test_load_pages_识别页面且不被代码块注释干扰(tmp_path):
    from wiki_aggregate import load_pages
    pages = load_pages(_fake_l1(tmp_path))
    assert [p["title"] for p in pages] == ["第一页标题", "第二页标题"]


def test_load_pages_把会话内序号提升为全局编号(tmp_path):
    from wiki_aggregate import load_pages
    pages = load_pages(_fake_l1(tmp_path))
    assert "[m101]" in pages[0]["body"] and "[m103]" in pages[0]["body"]
    assert "[m102]" in pages[1]["body"]
    assert pages[0]["mids"] == ["m101", "m103"]
    assert pages[0]["related"] == ["第二页标题", "不存在的页"]


def test_load_pages_数据库映射优先于产物对齐(tmp_path):
    """产物的 sources 数组可能列不全正文用到的编号，库里的映射才是权威。"""
    from wiki_aggregate import load_pages
    seq = {("src", "cid"): {1: "m900", 2: "m901", 3: "m902"}}
    pages = load_pages(_fake_l1(tmp_path), seq)
    assert "[m900]" in pages[0]["body"] and "[m902]" in pages[0]["body"]
    assert "[m901]" in pages[1]["body"]


def test_load_pages_没有库映射时不静默丢正文(tmp_path):
    """映射缺位时正文要保留下来（宁可留 `m?N` 标记，也不能丢内容）。"""
    from wiki_aggregate import load_pages
    pages = load_pages(_fake_l1(tmp_path), None)
    assert len(pages) == 2
    assert pages[0]["body"].strip()


# ---------------------------------------------------------------------------
# 链接修复
# ---------------------------------------------------------------------------

def _mkpage(d, name, title, aliases=(), body=""):
    """造一个带 frontmatter 的页面（wiki_linkfix 的测试夹具）。"""
    d.mkdir(parents=True, exist_ok=True)
    al = ("aliases: [%s]\n" % ", ".join('"%s"' % a for a in aliases)) if aliases else ""
    (d / name).write_text(
        "---\ntitle: %s\n%s---\n\n# %s\n%s\n" % (title, al, title, body),
        encoding="utf-8")


def test_linkfix_精确命中(tmp_path):
    from wiki_linkfix import collect_pages, resolve
    d = tmp_path / "l2"
    _mkpage(d, "001-页面-A.md", "页面 A")
    idx, _ = collect_pages(d)
    assert resolve("页面 A", idx, {}) == "001-页面-A"      # 按标题
    assert resolve("001-页面-A", idx, {}) == "001-页面-A"   # 按文件名


def test_linkfix_规范化后唯一命中(tmp_path):
    from wiki_linkfix import collect_pages, resolve
    d = tmp_path / "l2"
    _mkpage(d, "005-x.md", "ADB 命令实现中的 shell 注入防御约束：不要在命令里用管道")
    idx, _ = collect_pages(d)
    assert resolve("ADB命令实现中的shell注入防御约束-不要在命令里用管道", idx, {}) == "005-x"


def test_linkfix_按合并关系重定向(tmp_path):
    """第二级把第一级的多个页面合并成一页，旧标题要能重定向过去。"""
    from wiki_linkfix import collect_redirects, collect_pages, resolve
    d = tmp_path / "l2"
    _mkpage(d, "009-大页.md", "合并后的大页")
    (d / "009-大页.json").write_text(json.dumps(
        {"title": "合并后的大页", "from_titles": ["旧页 A", "旧页 B"]},
        ensure_ascii=False), encoding="utf-8")
    idx, _ = collect_pages(d)
    red = collect_redirects(d)
    assert resolve("旧页 A", idx, red) == "009-大页"
    assert resolve("旧页 B", idx, red) == "009-大页"


def test_linkfix_歧义时宁可不猜(tmp_path):
    """`AgentMemHub` 对应二十多个标题，猜错比留死链更糟。"""
    from wiki_linkfix import collect_pages, resolve
    d = tmp_path / "l2"
    _mkpage(d, "001-a.md", "AgentMemHub 数据库概览")
    _mkpage(d, "002-b.md", "AgentMemHub 配置体系")
    idx, _ = collect_pages(d)
    assert resolve("AgentMemHub", idx, {}) is None


def test_linkfix_重定向目标不存在时不算命中(tmp_path):
    from wiki_linkfix import collect_pages, resolve
    d = tmp_path / "l2"
    _mkpage(d, "001-真正的页.md", "真正的页")
    idx, _ = collect_pages(d)
    assert resolve("旧页", idx, {"旧页": "已被删掉的页"}) is None


def test_linkfix_输出文件名形态_Obsidian可解析(tmp_path):
    """**关键回归（硬性要求）**：Obsidian 的 `[[X]]` **只按文件名/路径解析，
    `aliases` 不参与**（2026-09-26 用 `obsidian eval` 实测：`getFirstLinkpathDest(
    "标题")` → null，而 `getFirstLinkpathDest("041-…")` → 命中）。

    所以改写必须输出 `[[<文件名>|<标题>]]` —— 输出 `[[标题]]` 的话全库都是死链，
    **点击就新建一个空白笔记**（实测 228 个目标 / 1136 条）。产物是给人看的，
    这条不能退让。
    """
    from wiki_linkfix import run_fix
    d = tmp_path / "l2"
    _mkpage(d, "041-RAG-与-LLM-Wiki-互补架构.md", "RAG 与 LLM Wiki：互补架构",
            body="见 [[RAG 与 LLM Wiki：互补架构]]。")
    r = run_fix(d)

    txt = (d / "041-RAG-与-LLM-Wiki-互补架构.md").read_text(encoding="utf-8")
    assert "[[041-RAG-与-LLM-Wiki-互补架构|RAG 与 LLM Wiki：互补架构]]" in txt, txt
    assert r["fixed"] == 1 and r["dropped"] == 0
    # 幂等：已是正确形态不该再改（否则每次更新都白写一遍全库）
    assert run_fix(d)["files_changed"] == 0


def test_linkfix_标题自带斜杠不被当成路径(tmp_path):
    """标题里可能自带 `/`（如 `视觉/OCR 定位`）—— 整串必须先当标题试，
    先拆路径就会把这类标题拆坏。"""
    from wiki_linkfix import collect_pages, resolve
    d = tmp_path / "l2"
    _mkpage(d, "020-定位.md", "界面元素定位：视觉/OCR 定位的缺陷与分工")
    idx, _ = collect_pages(d)
    assert resolve("界面元素定位：视觉/OCR 定位的缺陷与分工", idx, {}) == "020-定位"


def test_linkfix_无法确定时去链但保留文字(tmp_path):
    """歧义/无对应页 → 去掉链接标记（读者仍读得到文字，且不产生空白笔记）。"""
    from wiki_linkfix import run_fix
    d = tmp_path / "l2"
    _mkpage(d, "001-a.md", "页面 A", body="见 [[根本不存在的页]]。")
    r = run_fix(d)
    assert "根本不存在的页" in (d / "001-a.md").read_text(encoding="utf-8")
    assert "[[" not in (d / "001-a.md").read_text(encoding="utf-8")
    assert r["dropped"] == 1


def test_collect_redirects_从产物建表(tmp_path):
    from wiki_linkfix import collect_redirects
    d = tmp_path / "l2"
    d.mkdir()
    (d / "a.json").write_text(json.dumps(
        {"title": "合并后的大页", "from_titles": ["旧页 A", "旧页 B"]},
        ensure_ascii=False), encoding="utf-8")
    # 下划线开头是运行期缓存文件，不该被当成页面
    (d / "_domains.json").write_text(json.dumps(
        {"title": "不该收进来", "from_titles": ["噪音"]}, ensure_ascii=False),
        encoding="utf-8")
    red = collect_redirects(d)
    # 重定向目标记的是**页面文件名 stem**（Obsidian 只认这个）
    assert red["旧页 A"] == "a" and red["旧页 B"] == "a"
    assert "噪音" not in red


def test_collect_pages_两级产出的标题都能收到(tmp_path):
    """第二级 frontmatter 是页面标题；第一级是"发起/会话 id"，标题在正文 `# ` 里。"""
    from wiki_linkfix import collect_pages
    d = tmp_path / "mix"
    d.mkdir()
    (d / "l2.md").write_text("---\ntitle: 页面标题\n---\n\n# 页面标题\n\n正文\n",
                             encoding="utf-8")
    (d / "l1.md").write_text("---\ntitle: src / cid\n---\n\n# 正文里的页标题\n\n*type: x*\n",
                             encoding="utf-8")
    idx, files = collect_pages(d)
    assert idx.lookup.get("页面标题") == "l2"
    assert idx.lookup.get("正文里的页标题") == "l1"
    assert len(files) == 2


def test_collect_pages_跳过索引文件(tmp_path):
    from wiki_linkfix import collect_pages
    d = tmp_path / "x"
    d.mkdir()
    (d / "index.md").write_text("# 知识库索引\n", encoding="utf-8")
    _, files = collect_pages(d)
    assert files == []


# ---------------------------------------------------------------------------
# 编译日志（logs/wiki.log）
#
# 长任务必须能从日志回答"跑到哪了 / 哪些失败了 / 花了多少 / 上次断在哪"，
# 所以这三条是流程可运维性的底线，不是锦上添花。
# ---------------------------------------------------------------------------

def test_wiki_日志能写能读(tmp_path, monkeypatch):
    from agentmemhub import logs
    monkeypatch.setattr(logs, "log_dir", lambda: tmp_path)
    logs.audit_wiki({"event": "call", "tag": "编译 ", "ok": True,
                     "usage": {"calls": 1, "completion": 100}})
    logs.audit_wiki({"event": "page_done", "title": "某页"})
    rows = logs.read_wiki_audit(tmp_path)
    assert len(rows) == 2
    assert rows[0]["event"] == "call" and rows[0]["ok"] is True
    assert rows[1]["title"] == "某页"


def test_wiki_日志坏行不影响读取(tmp_path):
    from agentmemhub import logs
    (tmp_path / "wiki.log").write_text(
        '{"event": "a"}\n这不是 json\n{"event": "b"}\n', encoding="utf-8")
    rows = logs.read_wiki_audit(tmp_path)
    assert [r["event"] for r in rows] == ["a", "b"]


def test_wiki_日志不存在时返回空(tmp_path):
    from agentmemhub import logs
    assert logs.read_wiki_audit(tmp_path) == []


def test_脚本写入的日志带时间戳(tmp_path, monkeypatch):
    from agentmemhub import logs
    from wiki_aggregate import _wlog
    monkeypatch.setattr(logs, "log_dir", lambda: tmp_path)
    _wlog(event="run_start", script="wiki_aggregate", note="中文不乱码")
    rows = logs.read_wiki_audit(tmp_path)
    assert rows[0]["event"] == "run_start"
    assert rows[0]["note"] == "中文不乱码"
    assert isinstance(rows[0]["ts"], float)


def test_日志写入失败不影响主流程(monkeypatch):
    """审计是旁路：日志炸了也绝不能让编译本身失败。"""
    from agentmemhub import logs
    from wiki_aggregate import _wlog

    def boom():
        raise OSError("磁盘满了")

    monkeypatch.setattr(logs, "log_dir", boom)
    _wlog(event="x")          # 不应抛出


# ---------------------------------------------------------------------------
# 分组分批：101 条一次性分组会卡死（2026-09-21 实测）
# ---------------------------------------------------------------------------

class _RecordingPlanner:
    """记录每次分组调用看到的条数，并为本批全部成员返回一个组。"""

    def __init__(self):
        self.seen: list[int] = []

    def complete_json(self, system, user, max_tokens=0, temperature=0.0):
        n = len([ln for ln in user.splitlines() if ln.startswith("[")])
        self.seen.append(n)
        return {"session_summary": "概要",
                "groups": [{"title": "组", "type": "concept",
                            "members": list(range(1, n + 1)), "why": "w"}],
                "unclassified": [{"n": n, "why": "不值得"}]}


def _mems(n):
    return [{"id": i, "type": "fact", "confidence": "high",
             "content": "内容 %d" % i} for i in range(1, n + 1)]


def test_分组分批_超限时切批且映射回全局编号():
    """批内编号是「批内 1 起」，必须映射回全局编号 —— 否则 compile_page 会按
    全局清单取错记忆，且越靠后的批次错得越离谱。"""
    from wiki_compile import plan_groups_batched
    c = _RecordingPlanner()
    plan = plan_groups_batched(c, _mems(5), log=lambda *a: None, batch=2)
    assert c.seen == [2, 2, 1]                      # 切成 3 批
    assert plan["groups"][0]["members"] == [1, 2]
    assert plan["groups"][1]["members"] == [3, 4]
    assert plan["groups"][2]["members"] == [5]
    assert [u["n"] for u in plan["unclassified"]] == [2, 4, 5]


def test_分组分批_未超限时保持单次调用():
    from wiki_compile import plan_groups_batched
    c = _RecordingPlanner()
    plan = plan_groups_batched(c, _mems(3), log=lambda *a: None, batch=10)
    assert c.seen == [3]
    assert plan["groups"][0]["members"] == [1, 2, 3]


def test_分组分批_batch为零时用模块默认上限():
    from wiki_compile import PLAN_BATCH_MAX, plan_groups_batched
    c = _RecordingPlanner()
    assert PLAN_BATCH_MAX >= 5
    plan_groups_batched(c, _mems(5), log=lambda *a: None, batch=0)
    assert c.seen == [5]                            # 5 条远小于默认上限 → 不分批


def test_分组上限_优先取配置且非法值回落(monkeypatch):
    """这几个阈值此前是**硬编码常量**：`wiki.*` 同名键虽在 DEFAULT_WIKI
    与 example 里存在，却从未被读取（默认值恰好一致才一直没暴露）。
    2026-09-22 配置审计补上最后一个漏网的 `workers`。"""
    from agentmemhub import config as _cfg
    import wiki_compile as _wc

    class _C:
        wiki = {"single_shot_max": 3, "plan_batch_max": 7, "page_workers": 5,
                "workers": 9}

    monkeypatch.setattr(_cfg, "config", lambda: _C())
    assert _wc._limits() == (3, 7, 5, 9)

    # 非法值（非整数 / 0 / 负数 / 空）一律回落默认
    _C.wiki = {"single_shot_max": "abc", "plan_batch_max": 0, "page_workers": -1,
               "workers": None}
    assert _wc._limits() == (_wc.SINGLE_SHOT_MAX, _wc.PLAN_BATCH_MAX,
                             _wc.PAGE_WORKERS, _wc.WORKERS)

    _C.wiki = {}
    assert _wc._limits() == (_wc.SINGLE_SHOT_MAX, _wc.PLAN_BATCH_MAX,
                             _wc.PAGE_WORKERS, _wc.WORKERS)


def test_compile_all的workers取配置(monkeypatch):
    """`wiki.workers` 此前有两处硬编码绕过配置：`compile_all` 的 `workers=4`
    默认值，与 `wiki.py` 里的 `workers or 4`。现在 0 表示"用配置"。"""
    from agentmemhub import config as _cfg
    import wiki_compile as _wc

    class _C:
        wiki = {"workers": 11}

    monkeypatch.setattr(_cfg, "config", lambda: _C())
    assert _wc._limits()[3] == 11
    # 显式传值优先于配置（参数 > 配置 > 常量）
    assert (7 or _wc._limits()[3]) == 7


def test_l2参数_优先取配置且非法值回落(monkeypatch):
    """`wiki.l2.workers` / `min_pages` / `domain_max` 此前**从未被读取** ——
    脚本只认 argparse 参数，而默认值恰好等于配置里的值，所以永远不报错。"""
    from agentmemhub import config as _cfg
    from wiki_aggregate import _l2_cfg, L2_WORKERS, L2_MIN_PAGES, L2_DOMAIN_MAX

    class _C:
        wiki_l2 = {"workers": 8, "min_pages": 5, "domain_max": 33}

    monkeypatch.setattr(_cfg, "config", lambda: _C())
    assert _l2_cfg("workers", L2_WORKERS) == 8
    assert _l2_cfg("min_pages", L2_MIN_PAGES) == 5
    assert _l2_cfg("domain_max", L2_DOMAIN_MAX) == 33

    # 非法 / 缺失一律回落常量
    _C.wiki_l2 = {"workers": 0, "min_pages": "x", "domain_max": None}
    assert _l2_cfg("workers", L2_WORKERS) == L2_WORKERS
    assert _l2_cfg("min_pages", L2_MIN_PAGES) == L2_MIN_PAGES
    assert _l2_cfg("domain_max", L2_DOMAIN_MAX) == L2_DOMAIN_MAX

    _C.wiki_l2 = None
    assert _l2_cfg("workers", L2_WORKERS) == L2_WORKERS


def test_组级并发_确实并发且结果按组序重排():
    """各组之间无依赖 → 并发编译；但**结果必须按组序重排**，否则页序会随完成
    顺序随机漂移，同一份记忆两次编译产出不同产物（不可复现）。"""
    import threading
    import time

    from wiki_compile import _compile_groups

    groups = [{"title": "G%d" % i, "members": [i + 1]} for i in range(6)]
    peak = {"cur": 0, "max": 0}
    lock = threading.Lock()

    class _C:
        def complete_json(self, system, user, max_tokens=0, temperature=0.0):
            with lock:
                peak["cur"] += 1
                peak["max"] = max(peak["max"], peak["cur"])
            time.sleep(0.05)                    # 模拟网络等待
            with lock:
                peak["cur"] -= 1
            return {"title": "T", "type": "concept", "summary": "s",
                    "body": "b", "related": []}

    out, failed = _compile_groups(_C(), _mems(6), groups, 4, log=lambda *a: None)
    assert peak["max"] > 1, "没有真正并发"
    assert [p["_members"][0] for p in out] == [1, 2, 3, 4, 5, 6], "页序漂移了"
    assert failed == []


def test_组级并发_单组失败不影响其它组并把失败成员报出来():
    """fail-open 语义在并发版必须保持（串行版本来就有）。

    同时**失败组的成员必须被报出来** —— 它们没进任何页面，调用方要据此把它们
    剔除出新基线；只 fail-open 不汇报，就等于静默丢弃（2026-09-21 切网实测）。
    """
    from wiki_compile import _compile_groups

    groups = [{"title": "G%d" % i, "members": [i + 1]} for i in range(4)]

    class _C:
        def complete_json(self, system, user, max_tokens=0, temperature=0.0):
            if "内容 2" in user:
                raise RuntimeError("这一组炸了")
            return {"title": "T", "type": "concept", "summary": "s",
                    "body": "b", "related": []}

    out, failed = _compile_groups(_C(), _mems(4), groups, 3, log=lambda *a: None)
    assert [p["_members"][0] for p in out] == [1, 3, 4]      # 只有第 2 组缺失
    assert failed == [2]                                     # 失败成员被报出


def test_组级并发_串行路径同样报出失败成员():
    from wiki_compile import _compile_groups

    groups = [{"title": "G%d" % i, "members": [i + 1]} for i in range(3)]

    class _C:
        def complete_json(self, system, user, max_tokens=0, temperature=0.0):
            if "内容 3" in user:
                raise RuntimeError("炸")
            return {"title": "T", "type": "concept", "summary": "s",
                    "body": "b", "related": []}

    out, failed = _compile_groups(_C(), _mems(3), groups, 1, log=lambda *a: None)
    assert [p["_members"][0] for p in out] == [1, 2]
    assert failed == [3]


def test_组级并发_并发度1时走串行结果一致():
    from wiki_compile import _compile_groups

    groups = [{"title": "G%d" % i, "members": [i + 1]} for i in range(3)]

    class _C:
        def complete_json(self, system, user, max_tokens=0, temperature=0.0):
            return {"title": "T", "type": "concept", "summary": "s",
                    "body": "b", "related": []}

    out, _failed = _compile_groups(_C(), _mems(3), groups, 1, log=lambda *a: None)
    assert [p["_members"][0] for p in out] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 顶层索引：链接必须指向知识页（.md），不是 json 副产物
# ---------------------------------------------------------------------------

_PAGE_MD = "\n".join([
    "---",
    "title: 页面甲",
    "domain: 域A",
    "type: concept",
    "compiled_at: 2026-09-22 12:00:00",
    "sources: 3",
    "merged_from: 2",
    "generator: wiki_aggregate prototype",
    "---",
    "",
    "# 页面甲",
    "",
    "**摘要**：这是页面甲的摘要。",
    "",
    "正文。",
])


def _domain(tmp_path: Path, dirname: str = "01-域A") -> Path:
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.md").write_text("# 域A\n", encoding="utf-8")
    return d


def test_顶层索引链接指向md而不是json(tmp_path):
    """实测踩过（2026-09-22 修复）：增量路径曾 `glob("*.json")` 并把 **json 文件名**
    拼进链接，导致顶层 index.md 的 362 条链接里 286 条指向 .json（点开是 JSON 原文）。
    域级 index 走 render_domain_index 另一条路径所以没被污染，坏链因此潜伏很久。
    """
    from wiki_aggregate import render_index_from_disk

    d = _domain(tmp_path)
    (d / "001-页面甲.md").write_text(_PAGE_MD, encoding="utf-8")
    (d / "001-页面甲.json").write_text(
        json.dumps({"title": "页面甲", "summary": "这是页面甲的摘要。"},
                   ensure_ascii=False), encoding="utf-8")

    text = render_index_from_disk(tmp_path, [], set())

    assert ".json" not in text, "顶层索引里不允许出现 json 链接"
    assert "(01-域A/001-页面甲.md)" in text
    assert "页面甲" in text and "这是页面甲的摘要。" in text


def test_顶层索引_json缺失时回落md自身(tmp_path):
    """json 是派生副产物，丢了不该让页面从索引里消失。"""
    from wiki_aggregate import render_index_from_disk

    d = _domain(tmp_path, "02-域B")
    (d / "007-页面乙.md").write_text(
        _PAGE_MD.replace("页面甲", "页面乙"), encoding="utf-8")

    text = render_index_from_disk(tmp_path, [], set())

    assert "(02-域B/007-页面乙.md)" in text
    assert "页面乙" in text
    assert "这是页面乙的摘要。" in text


def test_顶层索引跳过index本身(tmp_path):
    """各域的 index.md 是导航不是知识页，不该被列进顶层索引。"""
    from wiki_aggregate import render_index_from_disk

    d = _domain(tmp_path)
    (d / "001-页面甲.md").write_text(_PAGE_MD, encoding="utf-8")

    text = render_index_from_disk(tmp_path, [], set())

    assert "index.md" not in text


# ---------------------------------------------------------------------------
# 全量清理：只清域目录，不碰用户的东西
# ---------------------------------------------------------------------------

def test_清理旧域目录时不动隐藏目录(tmp_path):
    """实测踩过（2026-09-22）：产物目录被直接当 Obsidian vault 用时，
    `.obsidian/` 会被"凡是不在本轮 keep 里的目录一律 rmtree"一起删掉 —— 不可逆。
    隐藏目录是用户的编辑器配置，不是本脚本的派生物。
    """
    from wiki_aggregate import _prune_stale_domain_dirs

    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "01-旧域").mkdir()
    (tmp_path / "02-本轮域").mkdir()
    (tmp_path / "用户自建目录").mkdir()

    removed = _prune_stale_domain_dirs(tmp_path, {"02-本轮域"}, partial=False)

    assert removed == 1
    assert not (tmp_path / "01-旧域").exists(), "不属于本轮的域目录应被清掉"
    assert (tmp_path / "02-本轮域").exists(), "本轮域目录必须保留"
    assert (tmp_path / ".obsidian" / "app.json").exists(), "隐藏目录不能被删"
    assert (tmp_path / "用户自建目录").exists(), "非域目录（用户自建）不能被删"


def test_全量模式keep为空时绝不删任何目录(tmp_path):
    """⚠️ **最危险的一条**：`keep` 为空 = 本轮一个域都没写出来（归域/编译整体失败，
    比如 LLM 挂了、网络断了）。

    这时"清掉不属于本轮的目录"在字面上等于**清空整个知识库** —— 而且降级闸
    （`_is_degraded_run`）**拦不住**：它判的是"降级页占比"，而 `n_tasks == 0` 时
    那个比例无从计算，函数直接返回 False。

    产物虽然可由快照恢复，但"可恢复"不等于"该发生"。（2026-10-04 体检发现）
    """
    from wiki_aggregate import _prune_stale_domain_dirs

    for name in ("01-域甲", "02-域乙", "03-域丙"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "001-页.md").write_text("# 页", encoding="utf-8")

    removed = _prune_stale_domain_dirs(tmp_path, set(), partial=False)

    assert removed == 0, "keep 为空时必须**一个都不删**"
    assert sorted(d.name for d in tmp_path.iterdir()) == ["01-域甲", "02-域乙", "03-域丙"], \
        "整个知识库必须原样保留（否则一次 LLM 全线故障就会清空产物）"
    assert (tmp_path / "02-域乙" / "001-页.md").exists()


def test_增量模式绝不清目录(tmp_path):
    """增量只编了少数域，其它域产物都在磁盘上 —— 清了等于删掉整个知识库。"""
    from wiki_aggregate import _prune_stale_domain_dirs

    (tmp_path / "01-旧域").mkdir()
    (tmp_path / "02-本轮域").mkdir()

    assert _prune_stale_domain_dirs(tmp_path, {"02-本轮域"}, partial=True) == 0
    assert (tmp_path / "01-旧域").exists()


def test_已有域目录只认数字前缀(tmp_path):
    """`_existing_domain_dirs` 与 `_prune_stale_domain_dirs` 同判据：
    隐藏目录（用户的 `.obsidian/`）与用户自建目录都不是"上一轮产物"。"""
    from wiki_aggregate import _existing_domain_dirs

    (tmp_path / "01-域甲").mkdir()
    (tmp_path / "02-域乙").mkdir()
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "用户自建目录").mkdir()

    assert _existing_domain_dirs(tmp_path) == ["01-域甲", "02-域乙"]


def test_全量整域失败时必须中止写盘(tmp_path):
    """⚠️ 回归（2026-10-04 体检）：`keep` 为空那道闸只覆盖"**全失败**"，
    "**整域失败**"漏在外面。

    场景：全量重建时域甲的所有页都在**外层**异常（不是 fail-open，是彻底没产出，
    页任务走 `page_fail` 分支被 `continue` 掉）⇒ `by_domain_pages` 里没有甲
    ⇒ 甲不在 `keep` ⇒ `_prune_stale_domain_dirs` 把 `01-域甲` 当"上一轮残留"
    删掉，而本轮并没有替代产物 —— **净损失一整域**。同时其余域按成功域重新编号，
    `01-新域` 与 `01-旧域` 并存，索引与互链全乱。

    正确做法是本轮**整个不写盘**（与降级闸同源）：宁可什么都不做，
    也不要写出一个半成品知识库。首次构建（磁盘上没有旧产物）例外——无物可毁。
    """
    from wiki_aggregate import _domain_missing_abort

    for name in ("01-域甲", "02-域乙"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "001-页.md").write_text("# 页", encoding="utf-8")

    planned = {"域甲", "域乙", "域丙"}
    produced = {"域乙"}                          # 甲、丙整域没产出

    assert _domain_missing_abort(planned, produced, tmp_path) is True, \
        "有域整域没产出且磁盘上已有旧产物 ⇒ 必须中止，否则旧域产物被当残留清掉"

    assert _domain_missing_abort(planned, planned, tmp_path) is False, \
        "全部域都产出了 → 正常写盘"

    assert _domain_missing_abort(planned, produced, tmp_path / "还没有产物") is False, \
        "首次构建无物可毁 → 能写多少写多少，不为一两个失败的域让整轮白跑"


def test_全量重跑的页数崩塌改判整轮覆盖(tmp_path):
    """⚠️ 回归（2026-10-04 实测）：全量重跑会**重新划分域**，某个域从 61 页变 5 页
    是正常的域重构（内容被并到别的域去了），但逐域页数判据会：

      · **误拦** → 域结构永远收敛不了（实测产出 17 新域 + 4 旧域的混合体）；
      · **不自愈** → 下轮比较基准仍是那批旧产物，同样"崩塌"，死循环。

    所以全量模式下不再按页数判，改看**整轮内容覆盖**（见 `_run_coverage`）。
    """
    from wiki_aggregate import _is_page_collapse

    # 增量（默认 strict）仍按页数判：域结构稳定时页数骤降就是降级
    assert _is_page_collapse(61, 5) is True
    assert _is_page_collapse(61, 40) is False
    assert _is_page_collapse(2, 1) is False, "小域（旧页数 < COLLAPSE_MIN_OLD）不判"

    # 全量（strict=False）不按页数判 —— 交给整轮覆盖闸
    assert _is_page_collapse(61, 5, strict=False) is False
    assert _is_page_collapse(109, 28, strict=False) is False


def test_整轮覆盖闸_覆盖够高放行_覆盖崩了拒写(tmp_path):
    """整轮覆盖闸的两个方向：正常域重构（内容并到别处）要放行，
    上游 LLM 大面积故障（内容大面积缺失）要拦住。"""
    import json

    from wiki_aggregate import _run_coverage

    # 旧产物：01-旧域 下 10 页，分别引用 m1..m10
    d = tmp_path / "01-旧域"
    d.mkdir()
    for i in range(1, 11):
        (d / ("p%02d.json" % i)).write_text(
            json.dumps({"title": "页%d" % i, "body": "内容 [m%d]" % i}),
            encoding="utf-8")

    # 本轮：内容被并进新域，页数少了但 10 条全覆盖
    full = [{"title": "新页", "body": " ".join("[m%d]" % i for i in range(1, 11))}]
    ok = _run_coverage(tmp_path, {"新域": full}, {"新域": "01-旧域"})
    assert (ok["old"], ok["new"]) == (10, 10) and ok["cover"] == 1.0
    assert ok["degraded"] is False, "覆盖满 —— 这是域重构，必须放行"

    # 本轮只剩 2 条 → 判降级
    thin = [{"title": "残页", "body": "[m1] [m2]"}]
    bad = _run_coverage(tmp_path, {"新域": thin}, {"新域": "01-旧域"})
    assert bad["cover"] == 0.2 and bad["degraded"] is True, "覆盖崩了必须拒写"

    # 磁盘上还没有任何旧产物（首次构建）→ 不该拦
    first = _run_coverage(tmp_path / "空目录", {"新域": thin}, {"新域": "01-旧域"})
    assert first["old"] == 0 and first["degraded"] is False, "首次构建无物可毁"


# ---------------------------------------------------------------------------
# frontmatter aliases：让 Obsidian 的 [[标题]] 能解析
# ---------------------------------------------------------------------------

def test_渲染页面写出aliases供Obsidian解析():
    """Obsidian 的 `[[X]]` 按**文件名或 frontmatter aliases** 解析、**不读 title**；
    而 `**相关**` 里全是 `[[标题]]`，且 L2 文件名带 `NNN-` 前缀 —— 不给 aliases，
    这些链接就全悬空。实测：补前 948 处链接 **0** 可解析，补后 **948/948**。
    """
    from wiki_aggregate import render_page

    page = {"title": "ADBController 核心设计与功能清单", "type": "entity",
            "summary": "摘要", "body": "正文", "related": [], "sources": [],
            "from_titles": [], "from_files": []}
    text = render_page(page, "01-ADBController-与-Android-自动化")

    assert 'aliases: ["ADBController 核心设计与功能清单"]' in text
    assert text.index("aliases:") < text.index("---\n\n#"), "aliases 必须在 frontmatter 内"


def test_yaml标量转义():
    """title 来自 LLM，不能假设干净：引号/反斜杠必须转义，否则写出非法 YAML。"""
    from wiki_aggregate import _yaml_flow_scalar

    assert _yaml_flow_scalar('正常标题') == '"正常标题"'
    assert _yaml_flow_scalar('带"引号"的') == '"带\\"引号\\"的"'
    assert _yaml_flow_scalar("反\\斜杠") == '"反\\\\斜杠"'
    # 会被 YAML 当特殊含义开头的，也被引号保护住
    assert _yaml_flow_scalar("[看起来像列表]").startswith('"[')


# ---------------------------------------------------------------------------
# 写盘保护：LLM 故障时的降级结果不许覆盖好产物
# （2026-09-25 丢页事故：33 页的域被降级结果整域替换成 1 页，整轮丢 73 页）
# ---------------------------------------------------------------------------

def test_页数崩塌判据():
    """新页数远少于旧页数 → 判崩塌、拒写该域。小域要放行（别误伤）。"""
    from wiki_aggregate import _is_page_collapse

    assert _is_page_collapse(33, 1) is True          # 事故现场：33→1
    assert _is_page_collapse(44, 1) is True
    assert _is_page_collapse(10, 4) is True          # 低于一半
    assert _is_page_collapse(10, 5) is False         # 恰好一半：放行（正常合并）
    assert _is_page_collapse(10, 9) is False
    assert _is_page_collapse(3, 1) is False          # 小域不启用判据
    assert _is_page_collapse(0, 0) is False
    assert _is_page_collapse(2, 1) is False          # 旧页数不足 COLLAPSE_MIN_OLD


def test_整轮降级判据():
    """fail-open 降级页占比过半 → 整轮不写盘（比逐域判据更早的一闸）。"""
    from wiki_aggregate import _is_degraded_run

    assert _is_degraded_run(10, 10) is True          # 全降级（LLM 全挂）
    assert _is_degraded_run(5, 10) is True           # 恰好一半
    assert _is_degraded_run(4, 10) is False
    assert _is_degraded_run(0, 10) is False
    assert _is_degraded_run(0, 0) is False           # 空任务不算降级


def test_域目录整体换入(tmp_path):
    """写盘改为「先写临时目录、再整体换入」——旧页必须消失，且不留 .tmp_/.bak_ 垃圾。"""
    from wiki_aggregate import _count_domain_pages, _swap_domain_dir

    d = tmp_path / "01-域A"
    d.mkdir()
    (d / "001-旧页.md").write_text("旧", encoding="utf-8")
    (d / "002-旧页.md").write_text("旧", encoding="utf-8")
    (d / "index.md").write_text("# 域A", encoding="utf-8")
    (d / "001-旧页.json").write_text("{}", encoding="utf-8")
    assert _count_domain_pages(d) == 2

    _swap_domain_dir(tmp_path, d, {"001-新页.md": "新正文",
                                   "001-新页.json": "{}",
                                   "index.md": "# 域A"})

    names = sorted(f.name for f in d.iterdir())
    assert names == ["001-新页.json", "001-新页.md", "index.md"]
    assert (d / "001-新页.md").read_text(encoding="utf-8") == "新正文"
    assert _count_domain_pages(d) == 1
    leftovers = [f.name for f in tmp_path.iterdir() if f.name.startswith(".")]
    assert leftovers == [], "不该留下 .tmp_/.bak_ 临时目录"


def test_域目录换入失败时还原旧目录(tmp_path, monkeypatch):
    """换入中途失败必须把旧产物还原 —— 这正是"先删旧写新"做不到的事。"""
    from wiki_aggregate import _swap_domain_dir

    d = tmp_path / "01-域A"
    d.mkdir()
    (d / "001-旧页.md").write_text("旧", encoding="utf-8")

    real_rename = Path.rename
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] == 2:                 # 第 1 次（旧→bak）放行，第 2 次（tmp→正式）失败
            raise OSError("模拟磁盘故障")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError):
        _swap_domain_dir(tmp_path, d, {"001-新页.md": "新"})

    assert (d / "001-旧页.md").exists(), "换入失败必须还原旧产物"
    assert not (d / "001-新页.md").exists()
    assert not (tmp_path / ".bak_01-域A").exists()


def test_域目录计数忽略index与json(tmp_path):
    from wiki_aggregate import _count_domain_pages

    d = tmp_path / "01-域A"
    assert _count_domain_pages(d) == 0            # 目录不存在
    d.mkdir()
    (d / "index.md").write_text("x", encoding="utf-8")
    (d / "001-页.md").write_text("x", encoding="utf-8")
    (d / "001-页.json").write_text("{}", encoding="utf-8")
    assert _count_domain_pages(d) == 1


# ---------------------------------------------------------------------------
# 域内细分分批（2026-09-25：大域一次调用必撞 524 → 整域压成一页的根治）
# ---------------------------------------------------------------------------

def _members(n: int) -> list[dict]:
    return [{"title": "P%d" % i, "summary": "摘要%d" % i} for i in range(1, n + 1)]


def test_细分分批_批内编号映射回全局():
    """**关键回归**：批内编号是「批内 1 起」，不映射回全局就会取错成员，
    且越靠后的批次错得越离谱（与第一级 `plan_groups_batched` 同一个坑）。"""
    from wiki_aggregate import stage_plan

    class _C:
        def complete_json(self, system, user, **kw):
            n = len([ln for ln in user.splitlines() if ln.startswith("[")])
            return {"pages": [{"title": "批%d" % n,
                               "members": list(range(1, n + 1))}]}

    plans = stage_plan(_C(), {"name": "域A"}, _members(7), batch=3,
                       log=lambda *_: None)

    assert sorted(i for pl in plans for i in pl["idx"]) == list(range(1, 8))
    assert len(plans) == 3
    # 第二批覆盖全局 4~6（若忘了映射就会是 1~3）
    assert plans[1]["idx"] == [4, 5, 6]


def test_细分分批_某批失败只让该批成员各自成页():
    """**这正是丢掉「整域退化为一个页面」的关键**：一条失败只影响它覆盖的成员，
    其余批次照常合并 —— 43 页的域不会再被压成 1 页。"""
    from wiki_aggregate import stage_plan

    class _C:
        def complete_json(self, system, user, **kw):
            if "[1] P1" in user:                 # 只让第一批失败（重试也一致）
                raise RuntimeError("HTTP 524")
            return {"pages": [{"title": "合并页", "members": [1, 2]}]}

    plans = stage_plan(_C(), {"name": "域A"}, _members(6), batch=2,
                       log=lambda *_: None)

    assert sorted(pl["title"] for pl in plans) == ["P1", "P2", "合并页", "合并页"]
    assert sorted(i for pl in plans for i in pl["idx"]) == list(range(1, 7)), \
        "无论哪批失败都不能丢成员"


def test_细分分批_成员数不超过上限时仍是单批():
    from wiki_aggregate import plan_requests

    assert len(plan_requests("域A", "域A", _members(3), 40)) == 1
    assert len(plan_requests("域A", "域A", _members(40), 40)) == 1
    assert len(plan_requests("域A", "域A", _members(41), 40)) == 2


def test_细分请求的custom_id自带定位信息():
    """离线批处理里「上传」与「回读」可能隔几小时甚至换机器，凭据必须能独立定位。"""
    from wiki_aggregate import plan_requests

    reqs = plan_requests("DSH-Hermes", "DSH/Hermes 助手架构与运行", _members(5), 2)

    assert [r.custom_id for r in reqs] == [
        "l2plan|DSH-Hermes|0", "l2plan|DSH-Hermes|1", "l2plan|DSH-Hermes|2"]
    assert all(r.system and r.user for r in reqs)
    assert "DSH/Hermes 助手架构与运行" in reqs[0].user


def test_细分可换运行器_离线接缝():
    """`runner` 是离线批处理的唯一接缝：换成会写 JSONL 的实现即可，
    `stage_plan` 本身不必改。"""
    from wiki_aggregate import stage_plan

    seen = []

    class _Deferred:
        def run(self, requests):
            seen.extend(r.custom_id for r in requests)
            return [{} for _ in requests]        # 假装结果还没回来 → 成员各自成页

    plans = stage_plan(None, {"name": "域A"}, _members(5), batch=2, runner=_Deferred())

    assert [c.rsplit("|", 1)[1] for c in seen] == ["0", "1", "2"]
    assert all(c.startswith("l2plan|域A-") for c in seen), "域名段应带短哈希防撞"
    assert sorted(pl["title"] for pl in plans) == ["P1", "P2", "P3", "P4", "P5"]


def test_域名段带哈希_防custom_id相撞():
    """不同域名可能 slug 成同一个串（只差标点），而 custom_id 是**离线批处理的
    凭据** —— 撞了就是"把 A 域的结果填进 B 域"，既花错钱又写错页。"""
    from wiki_aggregate import _domain_key, _slug

    n1, n2 = "DSH/Hermes 助手架构", "DSH-Hermes 助手架构"
    assert _slug(n1) == _slug(n2), "前提：这两个名字 slug 后确实相同"
    assert _domain_key(n1) != _domain_key(n2), "slug 相同时必须靠 crc32 后缀区分开"


# ---------------------------------------------------------------------------
# 单个域写盘失败**不得**拖垮整轮
#
# 2026-09-28 实测事故：用户在 Obsidian 里打开着 `04-AgentMemHub-记忆系统` 的两个
# 笔记 → `ddir.rename(bak)` 报 `PermissionError [WinError 5]` → 异常一路穿透到
# `wiki.py` 的整轮中止闸，**88 分钟的一轮里 L1 编完、L2 一个域都没写**。
# 修法：换入失败与页数崩塌走同一条"跳过该域"通路（保留旧产物 + 进失败清单 +
# 不进新基线 → 下轮自动重试），绝不中止整轮。
# ---------------------------------------------------------------------------

def _write_test_page(title: str, fname: str) -> dict:
    """够 `render_page()` / `render_domain_index()` 用的最小页面。"""
    return {"title": title, "type": "主题", "summary": "摘要", "body": "正文",
            "related": [], "sources": [], "from_titles": [title],
            "from_files": [fname], "_file": fname}


def test_域换入失败只跳过该域_不中止整轮(tmp_path, monkeypatch):
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    for name in ("01-域A", "02-域B"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "001-旧页.md").write_text("旧", encoding="utf-8")

    by_domain = {"域A": [_write_test_page("A 页", "001-A.md")],
                 "域B": [_write_test_page("B 页", "001-B.md")]}
    dir_of = {"域A": "01-域A", "域B": "02-域B"}
    fl = FailureLog(tmp_path / "failures.jsonl")

    real_swap = wa._swap_domain_dir

    def flaky(out, ddir, files):
        if ddir.name == "02-域B":            # 模拟"该域的文件被别的程序占着"
            raise PermissionError("[WinError 5] 拒绝访问。")
        return real_swap(out, ddir, files)

    monkeypatch.setattr(wa, "_swap_domain_dir", flaky)
    results, skipped, details = wa._write_domain_outputs(
        tmp_path, by_domain, dir_of, fl)

    assert [n for n, _ in results] == ["域A"], "写得进去的域必须照写"
    assert skipped == ["域B"], "写不进去的域要如实报出来（上层靠它剔出新基线）"
    assert "拒绝访问" in details[0], "跳过原因要能追溯到原始错误"
    assert (tmp_path / "02-域B" / "001-旧页.md").exists(), "旧产物必须保留"
    assert not (tmp_path / "02-域B" / "001-B.md").exists(), "不留半成品"
    assert "域B" in set(fl.targets("l2-write")), "失败要进清单，否则下轮无从补跑"


def test_页数崩塌仍走同一条跳过通路(tmp_path, monkeypatch):
    """重构保护：崩塌路径照样跳过该域，且**根本不去动磁盘**。"""
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    d = tmp_path / "01-域A"
    d.mkdir()
    for i in range(1, 5):                    # 旧 4 页 → 新 1 页 = 崩塌
        (d / ("%03d-旧.md" % i)).write_text("旧", encoding="utf-8")

    called = {"n": 0}

    def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("页数崩塌时不该尝试换入")

    monkeypatch.setattr(wa, "_swap_domain_dir", boom)
    fl = FailureLog(tmp_path / "failures.jsonl")
    results, skipped, _ = wa._write_domain_outputs(
        tmp_path, {"域A": [_write_test_page("新页", "001-新.md")]},
        {"域A": "01-域A"}, fl)

    assert results == [] and skipped == ["域A"]
    assert called["n"] == 0
    assert sorted(f.name for f in d.iterdir()) == [
        "001-旧.md", "002-旧.md", "003-旧.md", "004-旧.md"]
    assert "域A" in set(fl.targets("l2-write"))


def test_换入失败不留下tmp与bak垃圾(tmp_path, monkeypatch):
    """失败路径也要收干净：`.tmp_<域>` 就躺在 Obsidian vault 里（实测残留过 153 个文件）。"""
    import wiki_aggregate as wa

    d = tmp_path / "01-域A"
    d.mkdir()
    (d / "001-旧页.md").write_text("旧", encoding="utf-8")

    real_rename = Path.rename
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] == 2:                  # 第 1 次（旧→bak）放行，第 2 次（tmp→正式）失败
            raise PermissionError("[WinError 5] 拒绝访问。")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    err = wa._write_domain(tmp_path, d, {"001-新页.md": "新"})

    assert err, "失败必须返回可读原因而不是抛出去"
    assert (d / "001-旧页.md").read_text(encoding="utf-8") == "旧", "旧产物要还原"
    leftovers = sorted(f.name for f in tmp_path.iterdir() if f.name.startswith("."))
    assert leftovers == [], "不该留下 .tmp_/.bak_ 垃圾"


# ---------------------------------------------------------------------------
# 换入成功要**销账**历史失败（否则累积"幽灵失败"）
#
# 2026-10-08 实测：2026-10-04 14:32 增量轮因页数崩塌跳过 4 个域（保护性，
# 保留旧产物）→ 进失败清单；同一晚 17:14 全量轮按设计**重新划分域**
# （17 域 → 21 域）、完整写入 396 页、**零跳过** —— 那 4 个域的失败却一条都没销账，
# 一直挂到 10-08 人工清账。根因：销账只发生在 `--retry-failed` 路径，
# `_write_domain_outputs()` 换入成功时没有 `resolve()`。
# 判据：**失败清单是"待办"，磁盘上已经是最新一轮产物了，待办就该消。**
# ---------------------------------------------------------------------------

def test_换入成功即销账该域的历史失败(tmp_path):
    """本轮真写出来了 → 该域历史失败必须销账（否则幽灵条目会一直攒）。"""
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    d = tmp_path / "01-域A"
    d.mkdir()
    (d / "001-旧页.md").write_text("旧", encoding="utf-8")

    fl = FailureLog(tmp_path / "failures.jsonl")
    fl.record(stage="l2-write", target="域A", error="页数崩塌 4→1，已保留旧产物")
    assert fl.targets("l2-write") == ["域A"], "前置：清单里先有一条历史失败"

    results, skipped, _ = wa._write_domain_outputs(
        tmp_path, {"域A": [_write_test_page("新页", "001-新.md")]},
        {"域A": "01-域A"}, fl)

    assert [n for n, _ in results] == ["域A"] and skipped == []
    assert fl.targets("l2-write") == [], "本轮已真写盘，历史失败就该销账"


def test_本轮仍被跳过时历史失败必须保留(tmp_path):
    """反向保护：本轮**没**写出来的域，历史失败一条都不能销。"""
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    d = tmp_path / "01-域A"
    d.mkdir()
    for i in range(1, 5):                    # 旧 4 页 → 新 1 页 = 崩塌，仍被跳过
        (d / ("%03d-旧.md" % i)).write_text("旧", encoding="utf-8")

    fl = FailureLog(tmp_path / "failures.jsonl")
    fl.record(stage="l2-write", target="域A", error="域目录换入失败：PermissionError")

    wa._write_domain_outputs(
        tmp_path, {"域A": [_write_test_page("新页", "001-新.md")]},
        {"域A": "01-域A"}, fl)

    assert "域A" in set(fl.targets("l2-write")), \
        "本轮没写出来，历史失败不能被销掉（否则再也无从补跑）"


def test_销账只影响本域(tmp_path):
    """A 域写成功不能把 B 域的失败一起销掉（销账粒度是 stage+target）。"""
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    for name in ("01-域A", "02-域B"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "001-旧页.md").write_text("旧", encoding="utf-8")

    fl = FailureLog(tmp_path / "failures.jsonl")
    fl.record(stage="l2-write", target="域A", error="页数崩塌 4→1，已保留旧产物")
    fl.record(stage="l2-write", target="域B", error="页数崩塌 9→2，已保留旧产物")

    real_swap = wa._swap_domain_dir

    def flaky(out, ddir, files):
        if ddir.name == "02-域B":            # B 域换入失败 → 只有它该留着
            raise PermissionError("[WinError 5] 拒绝访问。")
        return real_swap(out, ddir, files)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(wa, "_swap_domain_dir", flaky)
        wa._write_domain_outputs(
            tmp_path,
            {"域A": [_write_test_page("A 页", "001-A.md")],
             "域B": [_write_test_page("B 页", "001-B.md")]},
            {"域A": "01-域A", "域B": "02-域B"}, fl)

    assert fl.targets("l2-write") == ["域B"], "只销写成功那个域的账"


# ---------------------------------------------------------------------------
# 幽灵失败的第二个来源：**域在重划中消失**
#
# `_write_domain_outputs()` 只遍历"本轮域集合"，所以"上一轮有、这一轮没有"的域
# （全量重划域时很常见：17 域 → 21 域那轮就消失了几个）身上的失败**永远没人销**
# —— 下一轮连它的名字都不会出现在 `by_domain_pages` 里。
# 判据：域目录都被剪了（产物侧已无此域）⇒ 针对它的写盘待办失效。
# ---------------------------------------------------------------------------

def _write_domains_file(out, names: list[str]) -> None:
    """写 `_domains.json` —— `_domain_dir_map()` 只认它来定"权威域名"。"""
    (out / "_domains.json").write_text(
        json.dumps([{"name": n} for n in names], ensure_ascii=False),
        encoding="utf-8")


def test_域被剪掉后针对它的历史失败要销账(tmp_path):
    """全量重划域让一个域消失 ⇒ 它目录被剪 ⇒ 针对它的 l2-write 待办必须销账。"""
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    (tmp_path / "01-旧域").mkdir()
    (tmp_path / "01-旧域" / "001-旧页.md").write_text("旧", encoding="utf-8")
    _write_domains_file(tmp_path, ["旧域", "新域"])

    fl = FailureLog(tmp_path / "failures.jsonl")
    fl.record(stage="l2-write", target="旧域", error="页数崩塌 9→2，已保留旧产物")

    wa.finalize_outputs(
        tmp_path,
        {"新域": [_write_test_page("新页", "001-新.md")]},
        dir_of={"新域": "01-新域"}, partial=False, fl=fl)

    assert not (tmp_path / "01-旧域").exists(), "不属于本轮的旧域目录要被剪掉"
    assert fl.targets("l2-write") == [], "域都没了，针对它的待办必须销账"


def test_域表里认不出的孤儿目录被剪时保守不销账(tmp_path):
    """反向保护：`_domain_dir_map()` 对不上 `_domains.json` 的目录**直接不收**。

    那类目录被剪时我们**故意不销账** —— 宁可留一条待办让人看见，
    也不能因为"认不出它是谁"就误销一条真问题。
    """
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    (tmp_path / "07-孤儿域").mkdir()
    (tmp_path / "07-孤儿域" / "001-旧页.md").write_text("旧", encoding="utf-8")
    _write_domains_file(tmp_path, [])            # 域表里没有它 ⇒ 反查不到域名

    fl = FailureLog(tmp_path / "failures.jsonl")
    fl.record(stage="l2-write", target="孤儿域", error="域目录换入失败：PermissionError")

    wa.finalize_outputs(
        tmp_path,
        {"新域": [_write_test_page("新页", "001-新.md")]},
        dir_of={"新域": "01-新域"}, partial=False, fl=fl)

    assert not (tmp_path / "07-孤儿域").exists(), "判据一致：孤儿目录照样被剪"
    assert fl.targets("l2-write") == ["孤儿域"], "认不出是谁，就不销账（宁缺勿滥）"


def test_增量模式绝不动别人的账(tmp_path):
    """增量轮只编少数域，**绝不清目录、也绝不给未参与的域销账**。"""
    from agentmemhub.failures import FailureLog

    import wiki_aggregate as wa

    (tmp_path / "01-域A").mkdir()
    (tmp_path / "01-域A" / "001-旧页.md").write_text("旧", encoding="utf-8")
    _write_domains_file(tmp_path, ["域A", "域B"])

    fl = FailureLog(tmp_path / "failures.jsonl")
    fl.record(stage="l2-write", target="域B", error="页数崩塌 9→2，已保留旧产物")

    wa.finalize_outputs(
        tmp_path,
        {"域A": [_write_test_page("A 页", "001-A.md")]},
        dir_of={"域A": "01-域A"}, partial=True, fl=fl)

    assert (tmp_path / "01-域A").exists(), "增量不动目录"
    assert fl.targets("l2-write") == ["域B"], "本轮没碰域B，它的账不能动"


# ---------------------------------------------------------------------------
# M2b-1：离线提交轮 —— 整个域集合**一次**交出去，现场落盘
# ---------------------------------------------------------------------------

def _pg(title: str) -> dict:
    """够 `plan_requests()` 用的最小成员（第一级页面）。"""
    return {"title": title, "summary": "摘要", "type": "主题", "body": "正文",
            "mids": [], "file": "%s.md" % title}


class _FakeBatchUploader:
    def __init__(self):
        self.uploads: list[str] = []
        self.creates: list[tuple[str, int]] = []

    def upload(self, jsonl_text, *, filename="input.jsonl"):
        self.uploads.append(jsonl_text)
        return {"id": "file-1"}

    def create(self, input_file_id, *, name="", n_requests=0):
        self.creates.append((name, n_requests))
        return {"id": "batch-1", "status": "Created", "expires_at": 999.0}


def test_离线提交轮_整体一次提交并落现场(tmp_path):
    """**一次提交**（不是逐域）：19 个域逐个提交会变成 19 个任务，对账成本爆炸。"""
    from agentmemhub.llm_batch import OfflineRunner, load_state

    from wiki_aggregate import STATE_NAME, _submit_offline

    up = _FakeBatchUploader()
    runner = OfflineRunner(uploader=up, model="mimo-v2.6-flash", stage="l2plan")
    dom_list = [("域A", [_pg("P1"), _pg("P2"), _pg("P3")]), ("域B", [_pg("Q1")])]

    status = _submit_offline(tmp_path, runner, dom_list, plan_batch=2)

    sub = status["offline_submitted"]
    assert sub["batch_id"] == "batch-1"
    assert sub["n_domains"] == 2
    assert sub["n_requests"] == 3, "域A 3 成员按 batch=2 切 2 条 + 域B 1 条"
    assert sub["domains"] == ["域A", "域B"]
    assert len(up.creates) == 1, "整体一次提交 = 只建一个任务"
    assert up.creates[0][1] == 3

    jobs = load_state(tmp_path / STATE_NAME)
    assert len(jobs) == 1
    job = jobs[0]
    assert sorted(job.domains()) == ["域A", "域B"]
    assert job.input_file_id == "file-1" and job.provider_status == "created"
    assert job.stage == "l2plan" and job.expire_at == 999.0
    assert set(job.items) == set(sub["pending_ids"]), "现场要能对上每一条请求"
    assert len(job.members["域A"]) == 3, "成员快照必须原样保留（回读时还原全局编号）"


def test_离线提交轮_域集合为空时不提交(tmp_path):
    from agentmemhub.llm_batch import OfflineRunner

    from wiki_aggregate import _submit_offline

    class _Up:
        def upload(self, *a, **k):
            raise AssertionError("空域集合不该上传")

        def create(self, *a, **k):
            raise AssertionError("空域集合不该建任务")

    status = _submit_offline(tmp_path, OfflineRunner(uploader=_Up()), [], plan_batch=2)
    assert status["offline_submitted"] == {}


def test_离线提交轮_运行器没抛Pending时如实报空(tmp_path):
    """实现有问题时**不假装提交成功** —— 宁可让运维看见"本轮什么都没提交"。"""
    from wiki_aggregate import _submit_offline

    class _Noop:
        submissions: list = []
        on_submitted = None

        def run(self, reqs):
            return []                       # 忘了抛 PendingError

    status = _submit_offline(tmp_path, _Noop(), [("域A", [_pg("P1")])], plan_batch=2)
    assert status["offline_submitted"] == {}, "没抛 Pending 就是没提交"
    assert status["written_domains"] == [] and status["pages"] == 0


# ---------------------------------------------------------------------------
# M2b-2：回读轮的两个纯函数（细分结果合并 + 编译请求生成）
# ---------------------------------------------------------------------------

def _member(title: str, body: str = "正文") -> dict:
    """细分/编译现场用的成员快照（比 `_pg` 多带 `body` / `source` / `cid`）。"""
    return {"title": title, "summary": "摘要-" + title, "type": "主题",
            "body": "%s [m1]" % body, "mids": [1], "file": "%s.md" % title,
            "source": "a", "cid": "c1"}


def test_合并细分结果_批内编号映射回全局编号():
    """细分的下标是**批内 1 起** —— 不按 `批号 × plan_batch` 偏移就会取错成员，
    而且越靠后的批次错得越离谱。"""
    from wiki_aggregate import merge_plan_results

    members = {"域A": [_member("P%d" % i) for i in range(1, 6)]}
    items = {"l2plan|域A-x|0": {"domain": "域A", "batch_no": 0},
             "l2plan|域A-x|1": {"domain": "域A", "batch_no": 1},
             "l2plan|域A-x|2": {"domain": "域A", "batch_no": 2}}
    results = {
        "l2plan|域A-x|0": {"ok": True, "data": {"pages": [
            {"title": "页1", "members": [1, 2]}]}},
        "l2plan|域A-x|1": {"ok": True, "data": {"pages": [
            {"title": "页2", "members": [1]}]}},        # 批内1 → 全局 3
        "l2plan|域A-x|2": {"ok": True, "data": {"pages": [
            {"title": "页3", "members": [1]}]}},        # 批内1 → 全局 5
    }

    plans = merge_plan_results(items, members, results, plan_batch=2)

    assert [n for n, _ in plans] == ["域A"]
    groups = plans[0][1]
    assert [[m["title"] for m in g["group"]] for g in groups] == [
        ["P1", "P2"], ["P3"], ["P5"], ["P4"]], "P4 没被任何页覆盖 → 各自成页"
    assert groups[0]["title"] == "页1"


def test_合并细分结果_某批失败时该批成员各自成页():
    """与实时路同一兜底：一批失败只影响那批覆盖的成员，**不丢内容、也不塌成一页**。"""
    from wiki_aggregate import merge_plan_results

    members = {"域A": [_member("P%d" % i) for i in range(1, 5)]}
    items = {"l2plan|域A-x|0": {"domain": "域A", "batch_no": 0},
             "l2plan|域A-x|1": {"domain": "域A", "batch_no": 1}}
    results = {"l2plan|域A-x|0": {"ok": True, "data": {"pages": [
        {"title": "页1", "members": [1, 2]}]}},
        "l2plan|域A-x|1": {"ok": False, "error": "HTTP 524"}}

    groups = merge_plan_results(items, members, results, plan_batch=2)[0][1]

    assert [[m["title"] for m in g["group"]] for g in groups] == [
        ["P1", "P2"], ["P3"], ["P4"]]


def test_编译请求_只把需要LLM的页交出去():
    """单成员页直接沿用源页（实时路也这么省），不占批量额度、不花钱。"""
    from wiki_aggregate import compile_requests

    plans = [("域A", [{"title": "单页", "group": [_member("P1")]},
                     {"title": "合并页", "group": [_member("P2"), _member("P3")]}])]

    reqs, items, members = compile_requests(plans, all_titles=["单页", "合并页"])

    assert len(reqs) == 1, "只有多成员页需要 LLM"
    cid = reqs[0].custom_id
    assert cid.startswith("l2page|域A-") and cid.endswith("|2")
    assert items[cid] == {"domain": "域A", "page_no": 2}
    assert [m["needs_llm"] for m in members["域A"]] == [False, True]
    assert [m["ti"] for m in members["域A"]] == [1, 2]
    # 现场必须存**页组成员**：落盘轮是另一次运行，那时 L1 页面已不在内存里
    assert [m["title"] for m in members["域A"][1]["group"]] == ["P2", "P3"]
    assert "P2" in reqs[0].user and "合并页" in reqs[0].user


def test_编译请求_单成员也强制重编时全部交出去():
    from wiki_aggregate import compile_requests

    plans = [("域A", [{"title": "单页", "group": [_member("P1")]}])]

    reqs, items, members = compile_requests(plans, all_titles=[], recompile_single=True)

    assert len(reqs) == 1 and members["域A"][0]["needs_llm"] is True


def test_编译提示词两路共用():
    """抽 `compile_prompt` 的意义：实时路与离线路发出去的是**同一份提示词**。"""
    from wiki_aggregate import compile_prompt

    system, user, dropped = compile_prompt("目标标题", [_member("P1"), _member("P2")],
                                           ["已有A", "已有B"])

    assert system
    assert "目标标题" in user and "以下是 2 个来源页面" in user
    assert "已有A" in user and "P1" in user
    assert dropped == 0


def test_编译段提交_消费细分结果后交出第二段(tmp_path):
    """M2b-2 的第二步：回读到的细分结果 → 合并 → 生成编译请求 → **再交一次**。

    这是三段式的中间一环：运行 1 交细分、运行 2 交编译、运行 3 才落盘。
    """
    from agentmemhub.llm_batch import BatchJob, OfflineRunner, load_state

    from wiki_aggregate import STATE_NAME, submit_offline_compile

    plan_job = BatchJob(
        batch_id="batch-plan", stage="l2plan", submitted_at=1.0,
        items={"l2plan|域A-x|0": {"domain": "域A", "batch_no": 0},
               "l2plan|域A-x|1": {"domain": "域A", "batch_no": 1},
               "l2plan|域B-x|0": {"domain": "域B", "batch_no": 0}},
        members={"域A": [_member("P1"), _member("P2"), _member("P3")],
                 "域B": [_member("Q1")]})
    results = {                            # 域A：批0 合成一页；批1 空 → P3 各自成页
        "l2plan|域A-x|0": {"ok": True, "data": {"pages": [
            {"title": "合并页", "members": [1, 2]}]}},
        "l2plan|域A-x|1": {"ok": True, "data": {"pages": []}},
        "l2plan|域B-x|0": {"ok": True, "data": {"pages": [
            {"title": "Q1", "members": [1]}]}},
    }
    up = _FakeBatchUploader()
    runner = OfflineRunner(uploader=up, model="m")

    status = submit_offline_compile(tmp_path, runner, plan_job, results,
                                    all_titles=["合并页", "P3", "Q1"], plan_batch=2)

    sub = status["offline_submitted"]
    assert sub["stage"] == "l2compile"
    assert len(up.creates) == 1 and up.creates[0][1] == 1, "只有'合并页'需要 LLM"

    jobs = load_state(tmp_path / STATE_NAME)
    assert len(jobs) == 1 and jobs[0].stage == "l2compile"
    assert jobs[0].batch_id == "batch-1"
    snap = jobs[0].members["域A"]
    assert [m["title"] for m in snap] == ["合并页", "P3"]
    assert [m["needs_llm"] for m in snap] == [True, False], \
        "单成员页本地生成，不进批量请求"
    assert [m["ti"] for m in snap] == [1, 2]
    assert next(iter(jobs[0].items)).startswith("l2page|域A-")


def test_编译段提交_没有需要LLM的页时不提交(tmp_path):
    """全部是单成员页时，编译段无事可做 —— 不该建一个空任务。"""
    from agentmemhub.llm_batch import BatchJob, OfflineRunner, load_state

    from wiki_aggregate import STATE_NAME, submit_offline_compile

    plan_job = BatchJob(batch_id="b1", items={"c": {"domain": "域A", "batch_no": 0}},
                        members={"域A": [_member("P1")]})
    results = {"c": {"ok": True, "data": {"pages": [
        {"title": "P1", "members": [1]}]}}}

    class _Up:
        def upload(self, *a, **k):
            raise AssertionError("没有需要 LLM 的页，不该上传")

        def create(self, *a, **k):
            raise AssertionError("没有需要 LLM 的页，不该建任务")

    status = submit_offline_compile(tmp_path, OfflineRunner(uploader=_Up()),
                                    plan_job, results, all_titles=[])

    assert status["offline_submitted"] == {}
    assert load_state(tmp_path / STATE_NAME) == []


# ---------------------------------------------------------------------------
# M2b-3：落盘轮 —— 把回读到的编译结果组装成页面并写盘
# ---------------------------------------------------------------------------

def test_组装页面_三条规则与实时路逐条对应():
    """单成员沿用源页 / 成功走 `_compile_page` / 失败 fail-open 拼接。"""
    from wiki_aggregate import _domain_key, assemble_pages

    members = {"域A": [
        {"ti": 1, "title": "单页", "needs_llm": False, "dropped": 0,
         "group": [_member("P1")]},
        {"ti": 2, "title": "合并页", "needs_llm": True, "dropped": 1,
         "group": [_member("P2"), _member("P3")]},
        {"ti": 3, "title": "失败页", "needs_llm": True, "dropped": 0,
         "group": [_member("P4"), _member("P5")]},
    ]}
    cid2 = "l2page|%s|2" % _domain_key("域A")
    results = {cid2: {"ok": True, "data": {"title": "合并后的页", "type": "entity",
                                           "summary": "摘要", "body": "正文",
                                           "related": ["别的页"]}}}

    by_domain, degraded = assemble_pages(members, results)

    pages = by_domain["域A"]
    assert [p["title"] for p in pages] == ["P1", "合并后的页", "失败页"]
    assert pages[0]["_file"] == "001-P1.md"
    assert pages[0]["body"] == "正文 [m1]", "单成员页原样沿用源页内容"
    assert pages[1]["type"] == "entity" and pages[1]["related"] == ["别的页"]
    assert pages[1]["from_titles"] == ["P2", "P3"]
    assert pages[1]["truncated"] == 1, "dropped 用现场存的，不重算"
    assert pages[1]["sources"] == ["m1"], "来源编号要补回 m 前缀"
    assert degraded == ["失败页"], "失败页计入降级（供整轮降级闸判断）"
    assert pages[2]["body"] == "正文 [m1]\n\n---\n\n正文 [m1]", "fail-open 直接拼接"
    assert pages[2]["from_titles"] == ["P4", "P5"]
    assert pages[2]["_file"] == "003-失败页.md"


def test_组装页面_域名取不到目录时也不会崩(tmp_path):
    from wiki_aggregate import assemble_pages

    by_domain, degraded = assemble_pages({"域A": [{"ti": 1, "needs_llm": True,
                                                  "group": []}]}, {})
    assert by_domain == {} and degraded == []


def test_落盘轮_沿用既有目录名并整域替换(tmp_path):
    from agentmemhub.failures import FailureLog

    from wiki_aggregate import write_offline_stage

    out = tmp_path / "out_l2"
    d = out / "01-域A"
    d.mkdir(parents=True)
    (d / "index.md").write_text("# 域A\n", encoding="utf-8")
    (d / "001-旧页.md").write_text("旧", encoding="utf-8")

    members = {"域A": [{"ti": 1, "title": "新页", "needs_llm": False, "dropped": 0,
                       "group": [_member("P1")]}],
               "域B": [{"ti": 1, "title": "B页", "needs_llm": False, "dropped": 0,
                       "group": [_member("Q1")]}]}
    fl = FailureLog(out / "failures.jsonl")

    status = write_offline_stage(out, members, {}, fl, partial=False)

    assert set(status["written_domains"]) == {"域A", "域B"}
    assert status["pages"] == 2 and status["degraded"] == 0
    assert (out / "01-域A" / "001-P1.md").exists(), "域A 必须沿用既有目录名"
    assert not (out / "01-域A" / "001-旧页.md").exists(), "整域替换：旧页应消失"
    new_dirs = [p.name for p in out.iterdir()
                if p.is_dir() and p.name[:1].isdigit() and p.name != "01-域A"]
    assert len(new_dirs) == 1 and new_dirs[0].startswith("02-"), "新域补号到 02"
    assert (out / "index.md").exists(), "顶层索引要写出"


def test_落盘轮_没有页面时什么都不写(tmp_path):
    from agentmemhub.failures import FailureLog

    from wiki_aggregate import write_offline_stage

    out = tmp_path / "out_l2"
    out.mkdir()

    status = write_offline_stage(out, {"域A": []}, {}, FailureLog(out / "f.jsonl"))

    assert status["written_domains"] == [] and status["pages"] == 0
    assert not (out / "index.md").exists()


def test_落盘轮_域名只认权威域表不认首行(tmp_path):
    """老产物的 `index.md` 首行写的是**目录名**（`# 04-AgentMemHub-记忆系统`）而不是
    域名 —— 域名绝不能照它定：会造出"假域名"写进 manifest，下一轮 `domain_map`
    把它当真域，于是又新建一套重复目录（2026-10-01 实测：manifest 从 18 个域涨到
    21 个，产物里出现 `20-04-AgentMemHub-记忆系统` 这种**双重前缀**目录）。

    域名一律取自 `_domains.json`（产物侧权威域表），再用 slug 反查目录。"""
    import json as _json

    from agentmemhub.failures import FailureLog

    from wiki_aggregate import write_offline_stage

    out = tmp_path / "out_l2"
    d = out / "04-AgentMemHub-记忆系统"
    d.mkdir(parents=True)
    (d / "index.md").write_text("# 04-AgentMemHub-记忆系统\n", encoding="utf-8")
    (d / "001-旧页.md").write_text("旧", encoding="utf-8")
    (out / "_domains.json").write_text(
        _json.dumps([{"name": "AgentMemHub 记忆系统", "members": [1]}],
                    ensure_ascii=False), encoding="utf-8")

    members = {"AgentMemHub 记忆系统": [
        {"ti": 1, "title": "新页", "needs_llm": False, "dropped": 0,
         "group": [_member("P1")]}]}
    fl = FailureLog(out / "failures.jsonl")

    status = write_offline_stage(out, members, {}, fl, partial=True)

    assert (d / "001-P1.md").exists(), "应复用既有目录（靠 slug 反查）"
    assert status["written_domains"] == ["AgentMemHub 记忆系统"]
    others = [p.name for p in out.iterdir()
              if p.is_dir() and p.name[:1].isdigit()
              and p.name != "04-AgentMemHub-记忆系统"]
    assert others == [], "不该新建重复目录（实测踩过：域目录 19 → 21）"


def test_落盘轮_不在权威域表里的目录不收(tmp_path):
    """域表里没有的目录（历史残留 / 假域名）**不参与**域名映射 —— 宁缺勿滥：
    假域名的危害（污染 manifest → 下轮又建重复目录）远大于少认一个目录。"""
    import json as _json

    from agentmemhub.failures import FailureLog

    from wiki_aggregate import _domain_dir_map

    out = tmp_path / "out_l2"
    (out / "20-04-AgentMemHub-记忆系统").mkdir(parents=True)   # 假域名产物
    (out / "04-AgentMemHub-记忆系统").mkdir(parents=True)      # 真域名产物
    (out / "_domains.json").write_text(
        _json.dumps([{"name": "AgentMemHub 记忆系统", "members": [1]}],
                    ensure_ascii=False), encoding="utf-8")

    m = _domain_dir_map(out)

    assert m == {"AgentMemHub 记忆系统": "04-AgentMemHub-记忆系统"}, \
        "只认权威域表能对上的目录，双重前缀目录应被忽略"


