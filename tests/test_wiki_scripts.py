# -*- coding: utf-8 -*-
"""LLM Wiki 生成流程的可复现性测试。

覆盖的是**纯函数与产物解析**——不调 LLM、不碰真数据库、不需要任何凭据，
所以任何人 clone 下来就能跑，用来确认"这套 wiki 生成流程在我的环境里可行"。
"""
import json
import sys
from pathlib import Path

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

def test_linkfix_精确命中():
    from wiki_linkfix import build_index, resolve
    titles = {"页面 A"}
    assert resolve("页面 A", titles, build_index(titles)) == "页面 A"


def test_linkfix_规范化后唯一命中():
    from wiki_linkfix import build_index, resolve
    titles = {"ADB 命令实现中的 shell 注入防御约束：不要在命令里用管道"}
    idx = build_index(titles)
    assert resolve("ADB命令实现中的shell注入防御约束-不要在命令里用管道",
                   titles, idx) == next(iter(titles))


def test_linkfix_按合并关系重定向():
    """第二级把第一级的多个页面合并成一页，旧标题要能重定向过去。"""
    from wiki_linkfix import build_index, resolve
    titles = {"合并后的大页"}
    idx = build_index(titles)
    red = {"旧页 A": "合并后的大页", "旧页 B": "合并后的大页"}
    assert resolve("旧页 A", titles, idx, red) == "合并后的大页"
    assert resolve("旧页 B", titles, idx, red) == "合并后的大页"


def test_linkfix_歧义时宁可不猜():
    """`AgentMemHub` 对应二十多个标题，猜错比留死链更糟。"""
    from wiki_linkfix import build_index, resolve
    titles = {"AgentMemHub 数据库概览", "AgentMemHub 配置体系"}
    assert resolve("AgentMemHub", titles, build_index(titles)) is None


def test_linkfix_重定向目标不存在时不算命中():
    from wiki_linkfix import build_index, resolve
    titles = {"真正的页"}
    idx = build_index(titles)
    assert resolve("旧页", titles, idx, {"旧页": "已被删掉的页"}) is None


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
    assert red == {"旧页 A": "合并后的大页", "旧页 B": "合并后的大页"}


def test_collect_titles_两级产出的标题都能收到(tmp_path):
    """第二级 frontmatter 是页面标题；第一级是"发起/会话 id"，标题在正文 `# ` 里。"""
    from wiki_linkfix import collect_titles
    d = tmp_path / "mix"
    d.mkdir()
    (d / "l2.md").write_text("---\ntitle: 页面标题\n---\n\n# 页面标题\n\n正文\n",
                             encoding="utf-8")
    (d / "l1.md").write_text("---\ntitle: src / cid\n---\n\n# 正文里的页标题\n\n*type: x*\n",
                             encoding="utf-8")
    titles, files = collect_titles(d)
    assert "页面标题" in titles and "正文里的页标题" in titles
    assert len(files) == 2


def test_collect_titles_跳过索引文件(tmp_path):
    from wiki_linkfix import collect_titles
    d = tmp_path / "x"
    d.mkdir()
    (d / "index.md").write_text("# 知识库索引\n", encoding="utf-8")
    titles, files = collect_titles(d)
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

