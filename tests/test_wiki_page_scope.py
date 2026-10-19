# -*- coding: utf-8 -*-
"""页级增量范围计算（`wiki._l2_page_scope`）。

2026-10-19 实测：12 条新记忆触发 3 个域共 **270 个 L2 页**重编、单轮近 1 小时，
且开销随知识库规模线性增长。L2 页 json 里的 `from_files` 是「L1 文件 → L2 页」
的反向映射，据此把重编量收窄到**真正受影响的那几页**。

这里守的是"收窄的边界"：
  · 只收引用了变动 L1 文件的页（不多编）；
  · 新 L1 文件必须并进本轮成员（否则新页无处安置）；
  · **定位不到就不收该域**（老产物没写 from_files / 目录不在 → 退回整域重编）。
    这条是安全底线：页级写盘只替换 scope 内那几页，scope 算错会留下
    "该删没删、该并没并"的旧页，比多编几页贵得多。
"""
from __future__ import annotations

import json

from agentmemhub.wiki import _l2_page_scope


def _mk_page(d, fname: str, frm) -> None:
    """造一个 L2 页产物（md + json；json 带 from_files）。"""
    (d / fname).write_text("x", encoding="utf-8")
    (d / fname.replace(".md", ".json")).write_text(
        json.dumps({"title": fname, "from_files": list(frm)},
                   ensure_ascii=False), encoding="utf-8")


def test_只收引用了变动L1文件的页(tmp_path):
    d = tmp_path / "01-域A"
    d.mkdir()
    _mk_page(d, "001-甲.md", ["mcp__a.md"])
    _mk_page(d, "002-乙.md", ["mcp__b.md"])

    sp, sm = _l2_page_scope(
        l2=tmp_path, dir_of={"域A": "01-域A"},
        touched={"mcp__b.md"}, new_files=set(),
        domain_map={"域A": ["mcp__a.md", "mcp__b.md"]})

    assert sp == {"域A": {"002-乙.md"}}, "只重编引用了变动 L1 文件的页"
    assert sm == {"域A": {"mcp__b.md"}}, "成员就是该页的 L1 来源"


def test_新L1文件也进入本轮成员(tmp_path):
    """新 L1 页若不在成员里就无处安置，下一轮还得再来一次。"""
    d = tmp_path / "01-域A"
    d.mkdir()
    _mk_page(d, "001-甲.md", ["mcp__a.md"])

    _sp, sm = _l2_page_scope(
        l2=tmp_path, dir_of={"域A": "01-域A"},
        touched={"mcp__a.md"}, new_files={"mcp__new.md"},
        domain_map={"域A": ["mcp__a.md", "mcp__new.md"]})

    assert sm["域A"] == {"mcp__a.md", "mcp__new.md"}


def test_定位不到就不收该域_退回整域重编(tmp_path):
    """老产物没有 from_files / 目录不存在 → 不收，调用方据此整域重编（保守）。"""
    d = tmp_path / "01-域A"
    d.mkdir()
    _mk_page(d, "001-甲.md", [])                # 没有 from_files 的老产物

    sp, sm = _l2_page_scope(
        l2=tmp_path,
        dir_of={"域A": "01-域A", "域B": "02-不存在"},
        touched={"mcp__a.md"}, new_files=set(),
        domain_map={"域A": ["mcp__a.md"], "域B": []})

    assert sp == {} and sm == {}, "算不出范围就必须整域重编，绝不猜"


def test_坏json不绊住整轮(tmp_path):
    d = tmp_path / "01-域A"
    d.mkdir()
    _mk_page(d, "001-甲.md", ["mcp__a.md"])
    (d / "002-坏.md").write_text("x", encoding="utf-8")
    (d / "002-坏.json").write_text("{坏掉的 json", encoding="utf-8")

    sp, sm = _l2_page_scope(
        l2=tmp_path, dir_of={"域A": "01-域A"},
        touched={"mcp__a.md"}, new_files=set(),
        domain_map={"域A": ["mcp__a.md"]})

    assert sp == {"域A": {"001-甲.md"}}, "坏页被跳过，好页照收"


def test_域索引文件不算页(tmp_path):
    d = tmp_path / "01-域A"
    d.mkdir()
    _mk_page(d, "001-甲.md", ["mcp__a.md"])
    (d / "index.md").write_text("# 域A", encoding="utf-8")

    sp, _sm = _l2_page_scope(
        l2=tmp_path, dir_of={"域A": "01-域A"},
        touched={"mcp__a.md"}, new_files=set(),
        domain_map={"域A": ["mcp__a.md"]})

    assert sp == {"域A": {"001-甲.md"}}


def test_只有新L1文件时也收该域_scope为空集只新增(tmp_path):
    """分桶后的第一轮：新桶文件是全新的，任何旧页都还没引用它。

    若这里判成"定位不到"而退回整域重编，分桶带来的收益会被整域重编吃掉。
    正确行为是：**收下该域、scope 为空集** —— 写盘侧保留全部旧页、只追加新页。
    """
    d = tmp_path / "01-域A"
    d.mkdir()
    _mk_page(d, "001-甲.md", ["mcp__a.md"])

    sp, sm = _l2_page_scope(
        l2=tmp_path, dir_of={"域A": "01-域A"},
        touched={"mcp__direct-2026-10-19.md"},
        new_files={"mcp__direct-2026-10-19.md"},
        domain_map={"域A": ["mcp__a.md", "mcp__direct-2026-10-19.md"]})

    assert sp == {"域A": set()}, "没有旧页要替换 → scope 是空集（只追加）"
    assert sm == {"域A": {"mcp__direct-2026-10-19.md"}}
