# -*- coding: utf-8 -*-
"""页面层准入策略测试：**高分优先 + 低分字面兜底**。

三轮实测得出的最终规则（详见 search.apply_page_policy 的 docstring）：

  · **分数不能当门槛**：页面融合分与相关性甚至反相关（长文本聚合分被压制）；
  · **证据也不能当门槛**：拼错场景真相关页只有弱信号，当门槛会把它们判死、
    把噪声放行；
  · **证据等级绝对优先也不行**（第二轮踩的坑）：查询 "github网络失败" 时，
    0.334/0.197 两条"只字面沾边"的页面占满席位，把真正对症的 0.848/0.841
    （无字面证据）挤了出去。

最终形态——**高分看分数，低分看字面证据，且字面有保底席位**：

  ① 候选按分数降序（高分语义相关优先）；
  ② 低于 ``literal_required_below`` 的页面必须有字面证据才算数；
  ③ ``literal_seats`` 个席位保底留给这类"低分模糊匹配"，否则中分池内页
     （拼错查询的「Clink」0.627）会按分数把它们全挤出去。

用户验收标准：**不遗漏 > 排序靠前 > 噪声可容忍**。本测试锁定该契约。
"""
from __future__ import annotations

from agentmemhub.rag.search import (apply_page_policy, page_evidence,
                                    reserve_pages)

#: 默认策略（与默认档 recall_level=3 展开后的 page_policy 一致）
POLICY = {"channel_k": 8, "max_in_results": 3, "literal_seats": 1,
          "literal_required_below": 0.7, "min_evidence": "any",
          "floor_ratio": 0.0, "reserve_seats": True}


def _cand(uid, channels):
    """构造 cand 条目：{channel: (rank, score)}"""
    return {uid: {ch: (i + 1, 0.9) for i, ch in enumerate(channels)}}


def _pages(kept, ids):
    return [u for u, _ in kept if u in ids]


# ---------------------------------------------------------------------------
# 证据等级判定（纯函数）
# ---------------------------------------------------------------------------

def test_证据等级判定():
    assert page_evidence({"page_fts": (1, 1.0)}) == "literal"
    assert page_evidence({"fts": (1, 1.0)}) == "literal"
    assert page_evidence({"ident": (1, 1.0)}) == "literal"
    assert page_evidence({"page": (1, 1.0), "vec": (5, 0.8)}) == "vector"
    assert page_evidence({"page": (1, 1.0), "vec:m2": (5, 0.8)}) == "vector"
    assert page_evidence({"page": (1, 1.0)}) == "pool"
    assert page_evidence({}) == "pool"
    assert page_evidence(None) == "pool"


# ---------------------------------------------------------------------------
# 核心回归：两条真实查询的数据
# ---------------------------------------------------------------------------

def test_核心回归_高分语义优先于低分字面():
    """实测 "github网络失败"：「网络环境确认与连接故障排查」0.848 **没有**字面
    证据，「GitHub 文件抓取方法」0.334 有——高分那条必须排在前且不被挤掉。
    （第二轮"证据等级绝对优先"正是在这里做反了。）"""
    ranked = [(1, 0.848), (2, 0.334)]
    cand = {**_cand(1, ["page", "vec"]), **_cand(2, ["page_fts"])}
    kept, dropped = apply_page_policy(ranked, cand, page_ids={1, 2}, policy=POLICY)
    assert [u for u, _ in kept] == [1, 2]        # 同为页面：高分在前
    assert dropped == []


def test_核心回归_低分字面页有保底席位():
    """实测 "windowsctrol"：中分池内噪声（0.627）会占席位，低分真相关页
    （0.026，靠残缺的 page_fts）必须靠 ``literal_seats`` 兜底救回。"""
    ranked = [(1, 0.627), (2, 0.026)]
    cand = {**_cand(1, ["page", "vec"]), **_cand(2, ["page_fts"])}
    kept, dropped = apply_page_policy(
        ranked, cand, page_ids={1, 2},
        policy={**POLICY, "max_in_results": 1, "literal_seats": 1,
                "literal_required_below": 0.5})
    assert [u for u, _ in kept] == [2]           # 唯一席位给字面兜底
    assert dropped == [1]


def test_核心回归_保底席位不挤掉高分语义():
    """配额 3 + 字面兜底 1 时：高分席与字面兜底席都能拿到位置。"""
    ranked = [(1, 0.848), (2, 0.841), (3, 0.334)]
    cand = {**_cand(1, ["page", "vec"]),
            **_cand(2, ["page", "vec"]),
            **_cand(3, ["page_fts"])}
    kept, _ = apply_page_policy(ranked, cand, page_ids={1, 2, 3}, policy=POLICY)
    assert set(_pages(kept, {1, 2, 3})) == {1, 2, 3}


def test_低分且无字面证据的页面不入选():
    """低分要靠字面证据兜底——没有字面证据的低分页（纯池内噪声）不该占席位。"""
    ranked = [(1, 0.9), (2, 0.2)]
    cand = {**_cand(1, ["page", "vec"]), **_cand(2, ["page"])}
    kept, dropped = apply_page_policy(ranked, cand, page_ids={1, 2}, policy=POLICY)
    assert [u for u, _ in kept] == [1]
    assert dropped == [2]


def test_非页面条目一律不受影响():
    ranked = [(1, 1.0), (2, 0.05)]
    cand = {**_cand(1, ["vec"]), **_cand(2, ["fts"])}
    kept, dropped = apply_page_policy(ranked, cand, page_ids=set(), policy=POLICY)
    assert kept == ranked and dropped == []


def test_配额_不超过总席位():
    ranked = [(i, 0.95) for i in range(1, 9)]        # 8 条高分页面候选
    cand = {}
    for i in range(1, 9):
        cand.update(_cand(i, ["page", "vec"]))
    kept, dropped = apply_page_policy(ranked, cand, page_ids=set(range(1, 9)),
                                      policy=POLICY)
    assert len(_pages(kept, set(range(1, 9)))) == 3  # 默认总席位 3
    assert len(dropped) == 5


def test_可选严格模式_只留字面证据():
    """``min_evidence`` 是证据硬门槛（档 1~2 会用到）；默认 any。"""
    policy = {**POLICY, "min_evidence": "literal"}
    ranked = [(1, 0.9), (2, 0.8), (3, 0.7)]
    cand = {**_cand(1, ["page"]),
            **_cand(2, ["page", "vec"]),
            **_cand(3, ["page_fts"])}
    kept, dropped = apply_page_policy(ranked, cand, page_ids={1, 2, 3},
                                      policy=policy)
    assert _pages(kept, {1, 2, 3}) == [3]
    assert sorted(dropped) == [1, 2]


def test_分数门限可选用_收紧到五成时低分页被剔除():
    """floor_ratio 默认 0（不设）；配置收紧为 0.5 时对页面再做一次分数过滤。"""
    policy = {**POLICY, "floor_ratio": 0.5}
    ranked = [(9, 1.0), (1, 0.4)]
    cand = {**_cand(9, ["vec"]), **_cand(1, ["page_fts"])}
    kept, dropped = apply_page_policy(ranked, cand, page_ids={1}, policy=policy)
    assert [u for u, _ in kept] == [9] and dropped == [1]


def test_保持排序与返回被剔除的页面():
    ranked = [(1, 1.0), (2, 0.97), (3, 0.96)]
    cand = {**_cand(1, ["vec"])}
    for i in (2, 3):
        cand.update(_cand(i, ["page", "page_fts"]))
    kept, dropped = apply_page_policy(ranked, cand, page_ids={2, 3}, policy=POLICY)
    assert [u for u, _ in kept] == [1, 2, 3]
    assert kept == sorted(kept, key=lambda x: -x[1])
    assert dropped == []


# ---------------------------------------------------------------------------
# 保底占位：入选页面不能被记忆挤出最终 k 条
# ---------------------------------------------------------------------------

def test_保位_页面被挤出k条时替换末位非页面():
    """相关页融合分 0.21×top，按分数排进不了前 k——选出来了却挤出去等于白做。"""
    ranked = [(9, 1.0), (8, 0.9), (1, 0.2)]
    top = [(9, 1.0), (8, 0.9)]                  # k=2，页面被挤掉
    out = reserve_pages(top, ranked, page_ids={1}, policy=POLICY)
    assert [u for u, _ in out] == [9, 1]        # 替换掉末位的 8
    assert len(out) == 2                        # 总数不变


def test_保位_不增加结果总数():
    """实测踩过的坑：追加会让 len(hits)=k+1，而下游（safe_cutoff 的
    `hits[:max_keep]` 窗口、面板 `top` 截断）都按前 N 条切，排在末尾的页面
    正好被切掉——k=8 时页面在窗口内正常，k=20 时页面消失。"""
    ranked = [(9, 1.0), (8, 0.9), (7, 0.8), (1, 0.2)]
    top = [(9, 1.0), (8, 0.9), (7, 0.8)]
    out = reserve_pages(top, ranked, page_ids={1}, policy=POLICY)
    assert len(out) == len(top) == 3
    assert 1 in [u for u, _ in out]
    assert out == sorted(out, key=lambda x: -x[1])      # 保持降序契约


def test_保位_已占位的页面计入配额():
    ranked = [(1, 1.0), (2, 0.9), (3, 0.8), (4, 0.7)]
    top = [(1, 1.0)]                            # 已有 1 条页面
    out = reserve_pages(top, ranked, page_ids={1, 2, 3, 4}, policy=POLICY)
    assert sum(1 for u, _ in out if u in {1, 2, 3, 4}) == 3     # 配额 3


def test_保位_关闭后不补位():
    policy = {**POLICY, "reserve_seats": False}
    out = reserve_pages([(9, 1.0)], [(9, 1.0), (1, 0.2)], page_ids={1},
                        policy=policy)
    assert [u for u, _ in out] == [9]


def test_保位_不重复补已在结果里的页面():
    out = reserve_pages([(1, 1.0)], [(1, 1.0)], page_ids={1}, policy=POLICY)
    assert [u for u, _ in out] == [1]
