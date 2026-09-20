# -*- coding: utf-8 -*-
"""短语精确匹配通道测试（解决中英混合专有名词的召回盲区）。

问题（实测）：库里 12 个含 "Command Code" 的单元，在查询 "command" 下
**11 个连候选池都没进**——原因是三件事叠加：
  · FTS 的 trigram 片段是 **OR** 连接的（"com" OR "omm" …），丢掉了"词组整体
    出现"这一强信号；
  · bm25 对长文本不利（记忆条目长）→ 被 FTS 的候选截断挤出；
  · 中文向量模型对英文短语（command code）语义匹配弱。

对照：`wait_for_element` 这种**单个长标识符**走 identifier 通道，召得很稳——
因为它有"字面精确证据"。本通道把同样的判据补给**多词短语**。
"""
from __future__ import annotations

import sqlite3

from agentmemhub.rag.search import (extract_phrases, identifier_search,
                                    phrase_search)


# ---------------------------------------------------------------------------
# 短语提取
# ---------------------------------------------------------------------------

def test_短语提取_只取多词英文短语():
    assert extract_phrases("command code") == ["command code"]
    assert extract_phrases("Command Code 定价调研") == ["Command Code"]
    assert extract_phrases("api key 与 provider 配置") == ["api key"]


def test_短语提取_单词与纯中文不是短语():
    """单个标识符走 identifier 通道，中文走 trigram——不在这里重复处理。"""
    assert extract_phrases("wait_for_element") == []
    assert extract_phrases("电脑窗口控制") == []
    assert extract_phrases("deskflow") == []


def test_短语提取_太短的组合被剔除():
    """短组合（"is a"）会引入海量噪声，必须过滤；够长的窗口保留。"""
    assert extract_phrases("is a") == []
    assert extract_phrases("a b") == []
    assert extract_phrases("is a test") == ["a test", "is a test"]  # "is a" 被过滤


def test_短语提取_连续英文串拆成滑动窗口():
    """查询 "deep seek command code" 整串当短语是废的（库里不会连着写五个词），
    必须拆成 2 词窗口——"command code" 才是真正会出现的词组。"""
    got = extract_phrases("deep seek command code")
    assert "command code" in got
    assert "deep seek" in got
    assert "deep seek command code" not in got


def test_短语提取_去重且限流():
    got = extract_phrases("api key api key command code")
    assert got.count("api key") == 1               # 去重（大小写不敏感）
    assert "command code" in got
    assert len(got) <= 4                           # 上限，防查询拼接


def test_短语提取_形态归一():
    assert extract_phrases("command   code") == ["command code"]   # 多空格归一
    # 连字符连接的是**单个词**（不是短语）——留给 identifier/向量通道
    assert extract_phrases("Command-Code 对比") == []


# ---------------------------------------------------------------------------
# 短语命中：必须"整串连续出现"
# ---------------------------------------------------------------------------

def _conn_with(texts: dict[int, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE units (id INTEGER PRIMARY KEY,"
                 " text TEXT, title TEXT)")
    for uid, text in texts.items():
        conn.execute("INSERT INTO units VALUES (?,?,?)", (uid, text, ""))
    return conn


def test_短语通道_只匹配整串出现():
    conn = _conn_with({
        1: "【LLM 网关选型】Command Code（api.commandcode.ai）定价调研",
        2: "这条只含 command 一个词（命令行的 command）",
        3: "code 单独出现也不行",
    })
    hits = phrase_search(conn, ["command code"], 10)
    assert [h[0] for h in hits] == [1]        # 只有整串出现的那条
    assert hits[0][1] == 1.0                  # 精确字面证据恒强分
    assert phrase_search(conn, [], 10) == []
    conn.close()


def test_短语通道_大小写与标题都算():
    conn = _conn_with({1: "无关正文"})
    conn.execute("UPDATE units SET title='COMMAND CODE 网关' WHERE id=1")
    assert [h[0] for h in phrase_search(conn, ["command code"], 10)] == [1]
    conn.close()


def test_短语通道_多短语命中数决定次序():
    conn = _conn_with({
        1: "包含 api key 一个短语",
        2: "同时含 api key 与 command code 两个短语",
    })
    hits = phrase_search(conn, ["api key", "command code"], 10)
    assert [h[0] for h in hits] == [2, 1]     # 命中更多短语的排前
    conn.close()


def test_短语通道_支持排除会话():
    conn = _conn_with({1: "Command Code 网关", 2: "Command Code 定价"})
    hits = phrase_search(conn, ["command code"], 10, exclude_ids={1})
    assert [h[0] for h in hits] == [2]
    conn.close()


def test_短语通道与标识符通道同源_都是恒强分():
    """两者都是"字面精确证据"：命中即 1.0，不参与分数排序的模糊地带。"""
    conn = _conn_with({1: "wait_for_element 用法"})
    assert identifier_search(conn, ["wait_for_element"], 5) == [(1, 1.0)]
    conn.close()
