# -*- coding: utf-8 -*-
"""MCP 的 feed 工具：投喂 / 列素材 / 健康快照。

本文件守四件事：

① **既有五工具契约一字未动** —— Skill 侧强依赖它们的 schema 与返回格式，
   新增的必须是**独立**的三个（这里连顺序与 required 一起钉住）；
② **不依赖记忆引擎** —— 索引没建好、引擎没起来，投喂照样得能用
   （feed 是本地文件处理，与索引毫无关系）；
③ **绝不污染 stdout** —— MCP 走 stdio JSON-RPC，stdout 就是协议流，
   而 `feed/service.py` 的 `log` 默认值恰恰是 `print`（CLI 场景）；
   照抄 CLI 写法 = 把日志混进协议 = 客户端解析直接失败；
④ 工具结果**如实回报**（成功 / 失败 / 幂等 / 被格式挡），不吞错。

**隔离**：feed 三段路径注入 tmp；引擎调用 mock；通知库随 conftest 的
`AGENTMEM_HUB_DATA_DIR` 落沙箱；推送出口另有进程级熔断。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from agentmemhub import memos_daemon
from agentmemhub.feed import service as svc
from agentmemhub.mcp_server import MCPHandler


class _FakeCfg:
    def __init__(self, root: Path):
        self.root = root
        self.paths = {"db": root / "materials.db", "store": root / "store",
                      "kb": root / "kb"}

    def feed_paths(self):
        return dict(self.paths)

    def feed(self):
        return {}

    @property
    def wiki(self):
        return {"out_l2": ""}


@pytest.fixture()
def feed_cfg(tmp_path, monkeypatch):
    cfg = _FakeCfg(tmp_path / "feed")
    monkeypatch.setattr(svc, "_cfg", lambda: cfg)
    return cfg


@pytest.fixture()
def offline(monkeypatch):
    """引擎离线（feed 工具本就不该受它影响 —— 这正是要测的）。"""
    monkeypatch.setattr(memos_daemon, "auth_state", lambda: None)


def _handler() -> tuple[MCPHandler, io.StringIO]:
    out = io.StringIO()
    return MCPHandler(stdin=io.StringIO(), stdout=out), out


def _req(method: str, params: dict | None = None, mid: int = 1) -> str:
    msg: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


def _call(h: MCPHandler, name: str, args: dict, mid: int = 1) -> dict:
    return h._dispatch(_req("tools/call", {"name": name, "arguments": args},
                            mid=mid))


def _text(r: dict) -> str:
    return r["result"]["content"][0]["text"]


def _md(tmp_path: Path, name="笔记.md", body="# 标题\n\n正文内容够长。\n") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ① 契约：原有五个不动，新增三个独立
# ---------------------------------------------------------------------------

def test_tools_list_原有五工具契约逐字不变():
    h, _ = _handler()
    tools = h._dispatch(_req("tools/list"))["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names[:5] == ["memory_search", "memory_recent", "memory_stats",
                         "memory_save", "memory_score"], names
    assert names[5:] == ["feed_add", "feed_list", "feed_status"], names
    by = {t["name"]: t for t in tools}
    assert by["memory_search"]["inputSchema"]["required"] == ["query"]
    assert by["memory_save"]["inputSchema"]["required"] == ["content"]
    assert by["memory_score"]["inputSchema"]["required"] == ["trace_id", "polarity"]
    assert by["feed_add"]["inputSchema"]["required"] == ["value"]
    assert by["feed_add"]["inputSchema"]["properties"]["input"]["enum"] == \
        ["path", "url", "data", "text"]


def test_未知工具仍是协议错误():
    h, _ = _handler()
    r = _call(h, "feed_nope", {})
    assert "error" in r and r["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# ② feed 工具不依赖记忆引擎
# ---------------------------------------------------------------------------

def test_feed工具在引擎离线时仍可用(feed_cfg, offline, tmp_path):
    """索引没建好、引擎没起来 —— 投喂**照样得能用**。

    对照：同一时刻记忆工具确实返回"引擎不可用"的指引（说明引擎检查本身还在，
    只是不被 feed 工具继承）。
    """
    h, _ = _handler()
    p = _md(tmp_path, "离线也能投.md")
    assert "投喂成功" in _text(_call(h, "feed_add",
                                     {"input": "path", "value": str(p)}))
    assert "投喂素材库" in _text(_call(h, "feed_status", {}))
    # 文案按后端而异（rag 后端是"记忆索引不可用…"，memos 后端是"记忆引擎未运行…"），
    # 所以断言"确实失败了"，而不是去钉某一句话
    assert _call(h, "memory_stats", {})["result"].get("isError") is True


# ---------------------------------------------------------------------------
# ③ stdout 是协议流，绝不能被日志污染
# ---------------------------------------------------------------------------

def test_stdout_不被feed过程日志污染(feed_cfg, offline, tmp_path, monkeypatch):
    """`feed/service.py` 的 `log` 默认是 `print`（CLI 场景）——
    若 MCP 这层照抄，`[+] 原件入 raw：…` 会混进 JSON-RPC，客户端直接解析失败。

    但过程日志**也不能丢**：它被收进了返回文本，模型由此看到"跑到哪一步"。
    """
    h, _ = _handler()
    p = _md(tmp_path, "不许打印.md")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    r = _call(h, "feed_add", {"input": "path", "value": str(p)})
    leaked = buf.getvalue()
    assert leaked == "", "feed 的过程日志漏到了 stdout：%r" % leaked[:200]
    text = _text(r)
    assert "投喂成功" in text
    assert "过程：" in text and "原件入 raw" in text


# ---------------------------------------------------------------------------
# ④ 四种输入形态与如实回报
# ---------------------------------------------------------------------------

def test_feed_add_text形态直接投喂正文(feed_cfg, offline):
    h, _ = _handler()
    r = _call(h, "feed_add", {"input": "text",
                              "value": "# 会议纪要\n\n讨论了投喂链路的边界。\n",
                              "filename": "会议纪要"})
    text = _text(r)
    assert "投喂成功" in text and "会议纪要" in text
    assert "doc_id：" in text and "交付物：" in text


def test_feed_add_path形态(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    p = _md(tmp_path, "路径形态.md", "# 路径形态\n\n正文内容够长。\n")
    text = _text(_call(h, "feed_add", {"input": "path", "value": str(p)}))
    assert "投喂成功" in text and "路径形态" in text


def test_feed_add_data形态_传base64字节(feed_cfg, offline):
    import base64
    h, _ = _handler()
    blob = base64.b64encode("# 内存笔记\n\n正文够长。\n".encode("utf-8")).decode()
    text = _text(_call(h, "feed_add", {"input": "data", "value": blob,
                                       "encoding": "base64",
                                       "filename": "内存笔记.md"}))
    assert "投喂成功" in text


def test_feed_add_data形态缺filename时明说要它(feed_cfg, offline):
    h, _ = _handler()
    text = _text(_call(h, "feed_add", {"input": "data", "value": "abc"}))
    assert "filename" in text


def test_feed_add_不传input时按值判型(feed_cfg, offline, tmp_path):
    """容错：URL 最明确，存在的文件次之，其余当正文（最常见的是"一段字"）。"""
    h, _ = _handler()
    assert "投喂成功" in _text(_call(h, "feed_add",
                                     {"value": "# 裸正文\n\n正文够长。\n"}))
    p = _md(tmp_path, "猜路径.md")
    assert "投喂成功" in _text(_call(h, "feed_add", {"value": str(p)}))


def test_feed_add_未知input形态明确报错(feed_cfg, offline):
    h, _ = _handler()
    text = _text(_call(h, "feed_add", {"input": "telepathy", "value": "x"}))
    assert "input 必须是" in text


def test_feed_add_格式被挡时给出允许清单(feed_cfg, offline):
    h, _ = _handler()
    text = _text(_call(h, "feed_add", {"input": "data", "value": "x",
                                       "filename": "程序.exe"}))
    assert "不支持的格式" in text and ".md" in text


def test_feed_add_路径不存在时如实报错(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    text = _text(_call(h, "feed_add",
                       {"input": "path", "value": str(tmp_path / "没有这个.md")}))
    assert "投喂失败" in text and "找不到文件" in text


def test_feed_add_dry_run一个字都不写(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    p = _md(tmp_path, "干跑.md")
    text = _text(_call(h, "feed_add", {"input": "path", "value": str(p),
                                       "dry_run": True}))
    assert "dry-run" in text
    assert not (feed_cfg.paths["db"]).exists(), "dry-run 不许建库"
    assert not list(feed_cfg.paths["kb"].rglob("*.md")) \
        if feed_cfg.paths["kb"].exists() else True


def test_feed_add_重复投喂报已投喂过(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    p = _md(tmp_path, "会重复.md")
    _call(h, "feed_add", {"input": "path", "value": str(p)})
    text = _text(_call(h, "feed_add", {"input": "path", "value": str(p)}))
    assert "已投喂过" in text


# ---------------------------------------------------------------------------
# 列表与状态
# ---------------------------------------------------------------------------

def test_feed_list_列出刚投的素材(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    _call(h, "feed_add",
          {"input": "path",
           "value": str(_md(tmp_path, "清单.md", "# 清单\n\n正文内容够长。\n"))})
    text = _text(_call(h, "feed_list", {}))
    assert "清单" in text and "published" in text


def test_feed_list_空库时说明且不报错(feed_cfg, offline):
    h, _ = _handler()
    text = _text(_call(h, "feed_list", {}))
    assert "没有符合条件" in text


def test_feed_status_概览含状态_白名单与体检(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    _call(h, "feed_add", {"input": "path", "value": str(_md(tmp_path, "状态.md"))})
    text = _text(_call(h, "feed_status", {}))
    assert "投喂素材库：共 1 条" in text
    assert "状态：" in text and "published 1" in text
    assert "格式白名单" in text and ".md" in text
    assert "产物体检：P0=0" in text


def test_feed_status_给doc_id看单条详情(feed_cfg, offline, tmp_path):
    h, _ = _handler()
    text = _text(_call(h, "feed_add", {"input": "path",
                                       "value": str(_md(tmp_path, "详情.md"))}))
    doc_id = text.split("doc_id：")[1].split("\n")[0].strip()
    detail = _text(_call(h, "feed_status", {"doc_id": doc_id}))
    assert "素材 %s" % doc_id in detail
    assert "原件：" in detail and "交付物：" in detail and "转换器：" in detail


def test_feed_status_未知doc_id是工具级错误(feed_cfg, offline):
    h, _ = _handler()
    r = _call(h, "feed_status", {"doc_id": "根本不存在"})
    assert r["result"].get("isError") is True
    assert "没有这条素材" in _text(r)


# ---------------------------------------------------------------------------
# 审计：新增工具同样进 logs/mcp.log（唯一分发点包一层，天然覆盖）
# ---------------------------------------------------------------------------

def test_feed工具调用进审计流(feed_cfg, offline, tmp_path):
    from agentmemhub.logs import read_mcp_audit
    h, _ = _handler()
    _call(h, "feed_add", {"input": "path", "value": str(_md(tmp_path, "审计.md")),
                          "note": "验证 feed 工具也被审计"})
    rows = read_mcp_audit()
    calls = [r for r in rows if r.get("tool") == "feed_add"
             and r.get("phase") == "call"]
    assert calls, "feed_add 的调用必须进 logs/mcp.log"
    assert calls[-1].get("args", {}).get("note") == "验证 feed 工具也被审计"
