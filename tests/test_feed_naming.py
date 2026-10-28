# -*- coding: utf-8 -*-
"""交付物文件名 = `<自然语言标题>-<doc_id>.md`（2026-10-28 改动）。

为什么要守这些：
- 早期是纯 `<doc_id>.md`，理由是"路径必须稳定"；代价是人打开 Obsidian /
  资源管理器只看到一串哈希。改成标题在前之后，**路径稳定性靠 doc_id 尾巴**，
  所以「按 doc_id 反查文件」必须仍然成立——这是本文件第一条要守住的东西。
- 标题会变（重转 / 改元数据），旧文件必须删；不删的话同一份素材在目录里
  躺两份，投影会把同内容算成两个页面，自己跟自己重复。
"""
from agentmemhub.feed.store import MaterialStore, kb_doc_of, kb_slug

DOC = "2c9471cdfe808c96"          # 真实形态：sha256 前 16 位


def _store(tmp_path):
    return MaterialStore(root=tmp_path / "store", kb_dir=tmp_path / "kb")


def test_标题在前_哈希在后(tmp_path):
    s = _store(tmp_path)
    assert s.kb_path_of(DOC, "Jenkins Docker 自动化部署实践") == \
        "Jenkins Docker 自动化部署实践-%s.md" % DOC


def test_没有标题时退回纯锚(tmp_path):
    """任何素材都必须有落点：没标题不能导致写不出文件。"""
    s = _store(tmp_path)
    assert s.kb_path_of(DOC) == "%s.md" % DOC
    assert s.kb_path_of(DOC, "") == "%s.md" % DOC
    assert s.kb_path_of(DOC, "   ") == "%s.md" % DOC


def test_非法字符被清掉():
    # `[]#^|` 是 Obsidian 的链接/标签/别名语法；`\\/:*?"<>` 是文件系统保留字符
    assert "[" not in kb_slug("PDF[v2022.12.20] 许可协议")
    assert "/" not in kb_slug("a/b\\c:d*e?f\"g<h>i|j")
    assert kb_slug("  A   B  ") == "A B"


def test_超长标题被截断且留余量():
    long = "字" * 200
    out = kb_slug(long)
    assert len(out) <= 60
    # 截断点不能留尾部空格/点（Windows 会拒掉以 `.` 或空格结尾的名字）
    assert not out.endswith((" ", "."))


def test_从文件名反解_doc_id():
    assert kb_doc_of("Jenkins Docker 自动化部署实践-%s.md" % DOC) == DOC
    assert kb_doc_of("%s.md" % DOC) == DOC
    # 标题里带连字符也不能把尾巴解错（贪婪匹配到最后一个 `-`）
    assert kb_doc_of("a-b-c-%s.md" % DOC) == DOC
    assert kb_doc_of("没有哈希的怪名字.md") == ""


def test_改名后旧文件被删_不会留两份(tmp_path):
    s = _store(tmp_path)
    first = s.publish_kb(DOC, "v1", title="旧标题")
    second = s.publish_kb(DOC, "v2", title="新标题")
    files = sorted(p.name for p in s.kb_dir.glob("*.md"))
    assert len(files) == 1, "同 doc_id 只能有一个交付物：%s" % files
    assert files[0] == "新标题-%s.md" % DOC
    assert not (s.kb_dir / ("旧标题-%s.md" % DOC)).exists()
    assert first != second


def test_按_doc_id_能找到改名后的文件(tmp_path):
    """这是最关键的一条：所有调用方都按 doc_id 取文件，不能靠拼路径。"""
    s = _store(tmp_path)
    s.publish_kb(DOC, "正文", title="自然语言标题")
    got = s.kb_file(DOC)
    assert got.is_file()
    assert got.name == "自然语言标题-%s.md" % DOC


def test_兼容迁移前的纯哈希文件名(tmp_path):
    """老库里的 `<doc_id>.md` 还没改名时，按 doc_id 仍要能找到。"""
    s = _store(tmp_path)
    s.kb_dir.mkdir(parents=True, exist_ok=True)
    legacy = s.kb_dir / ("%s.md" % DOC)
    legacy.write_text("旧格式", encoding="utf-8")
    assert s.kb_file(DOC) == legacy


def test_找不到时返回纯锚路径(tmp_path):
    """找不到不能抛异常：调用方一律用 `.is_file()` 判断存在性。"""
    s = _store(tmp_path)
    assert s.kb_file(DOC) == s.kb_dir / ("%s.md" % DOC)
