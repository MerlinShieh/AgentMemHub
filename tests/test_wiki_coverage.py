"""L1 编译的覆盖率与文件名安全测试。

两个都在 2026-11-16 的复核里发现：

1. **覆盖率**：1897 条输入里 39 条在 L1 页里既没成页、也没进 `unclassified`
   —— LLM 直接漏了，那些记忆在知识库视图里完全不可见。
2. **文件名**：`_out_stem` 把 cid 截到 60 字符，两个不同的长项目编码
   截断后可能撞成同一个文件、互相覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import wiki_compile  # noqa: E402


# ---------------------------------------------------------------------------
# 文件名：超长加 md5，避免截断撞车
# ---------------------------------------------------------------------------

def test_out_stem_keeps_short_cid_untouched():
    assert wiki_compile._out_stem("mcp", "--D-data-vibeCoding--") == \
        "mcp__--D-data-vibeCoding--"


def test_out_stem_appends_md5_when_too_long():
    long_cid = "--D-" + "a" * 80 + "--"
    stem = wiki_compile._out_stem("mcp", long_cid)
    body = stem.split("__", 1)[1]
    assert len(body) <= 60, "超长 cid 必须压到 60 字符内"
    # 结尾是 8 位十六进制（md5 前缀）
    tail = body.rsplit("-", 1)[-1]
    assert len(tail) == 8 and all(c in "0123456789abcdef" for c in tail)


def test_out_stem_distinguishes_two_long_cids():
    """两个只在尾部不同的长 cid 不能撞成同一个文件名（会互相覆盖）。"""
    a = "--D-data-vibeCoding-virtual-projects-second-brain-skill-AAAA--"
    b = "--D-data-vibeCoding-virtual-projects-second-brain-skill-BBBB--"
    assert wiki_compile._out_stem("mcp", a) != wiki_compile._out_stem("mcp", b)


def test_out_stem_is_stable():
    """同一 cid 必须每次得到同一个名字（否则 resume 判定会失效）。"""
    cid = "--D-" + "b" * 90 + "--"
    assert wiki_compile._out_stem("mcp", cid) == \
        wiki_compile._out_stem("mcp", cid)


def test_out_stem_truncation_prefers_path_boundary():
    """截断点回退到最近的 `-`，不把路径段切成半截（可读性）。"""
    cid = "--D-data-vibeCoding-virtual-projects-very-long-project-name-here--"
    body = wiki_compile._out_stem("mcp", cid).split("__", 1)[1]
    head = body.rsplit("-", 1)[0]
    assert not head.endswith("-"), "前缀不该以分隔符结尾（说明切在路径段中间）"
    # 去掉两端的前缀分隔符后，不该出现空段（那是截断留下的 `--`）
    assert all(head.strip("-").split("-")), "截断后不该出现空路径段"


# ---------------------------------------------------------------------------
# 覆盖率：模型漏掉的输入必须被补录
# ---------------------------------------------------------------------------

def _mems(n: int) -> list[dict]:
    return [{"id": 100 + i, "content": "第 %d 条记忆的内容" % i,
             "type": "fact", "confidence": "high"} for i in range(1, n + 1)]


def test_coverage_no_gap_when_pages_cover_all():
    result = {"pages": [{"sources": [1, 2, 3]}], "unclassified": []}
    assert wiki_compile._fill_coverage_gaps(result, _mems(3)) == []
    assert result["unclassified"] == []


def test_coverage_fills_missing_into_unclassified():
    """成页只覆盖了 1/3 → 剩下两条自动补进 unclassified，且带内容摘要。"""
    result = {"pages": [{"sources": [1]}], "unclassified": []}
    missing = wiki_compile._fill_coverage_gaps(result, _mems(3))
    assert missing == [2, 3]
    assert [u["n"] for u in result["unclassified"]] == [2, 3]
    assert all("自动补录" in u["why"] for u in result["unclassified"])
    assert all("第" in u["why"] for u in result["unclassified"])


def test_coverage_counts_existing_unclassified_as_covered():
    """已经进了 unclassified 的编号不该重复补录。"""
    result = {"pages": [{"sources": [1]}],
              "unclassified": [{"n": 2, "why": "纯过程性"}]}
    assert wiki_compile._fill_coverage_gaps(result, _mems(3)) == [3]
    assert [u["n"] for u in result["unclassified"]] == [2, 3]


def test_coverage_skips_failed_members():
    """组级失败的成员**不能**补录 —— 它们要走 failed_ids 让调用方重试，
    补录等于谎报"已编译"，下次 align 会永久掩盖失败。"""
    result = {"pages": [{"sources": [1]}], "unclassified": [],
              "failed_members": [2]}
    assert wiki_compile._fill_coverage_gaps(result, _mems(3)) == [3]
    assert [u["n"] for u in result["unclassified"]] == [3]


def test_coverage_accepts_members_key():
    """分组阶段产出的组用 `_members`（不是 `sources`），也要算已覆盖。"""
    result = {"pages": [{"_members": [1, 2, 3]}], "unclassified": []}
    assert wiki_compile._fill_coverage_gaps(result, _mems(3)) == []


def test_coverage_never_raises_on_empty_pages():
    result = {"pages": [], "unclassified": []}
    assert wiki_compile._fill_coverage_gaps(result, _mems(2)) == [1, 2]


def test_coverage_tolerates_unclassified_none():
    """上游把 unclassified 写成 None（缺字段不等于空列表）时不能崩。"""
    result = {"pages": [{"sources": [1]}], "unclassified": None}
    assert wiki_compile._fill_coverage_gaps(result, _mems(2)) == [2]
    assert [u["n"] for u in result["unclassified"]] == [2]
