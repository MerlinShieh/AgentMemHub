# -*- coding: utf-8 -*-
"""记忆操作事实流（`logs/memory.log`）测试。

三个事实流的分工（见 `logs.py` 的说明）：
  · `mcp.log`    —— **协议层**：谁调了什么工具、参数、耗时、成败
  · `memory.log` —— **数据层**：哪条记忆被写/读、**内容是什么**、从哪条路径来
  · `wiki.log`   —— **流水线层**：编译跑到哪、花了多少、断在哪

本文件锁定这个新事实流的契约：字段齐全（尤其**内容全文**）、来源路径标记、
超限轮转、以及"审计失败绝不炸主流程"。

日志目录由 `conftest` 的 autouse 夹具指向临时目录，本文件不碰真实 `logs/`。
"""
from __future__ import annotations

import sqlite3

from agentmemhub import logs


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def test_写入与读回():
    logs.audit_memory({"event": "write", "ts": 1.0, "memory_id": 7,
                       "content": "一条测试记忆", "chars": 6})
    rows = logs.read_memory_audit()
    assert len(rows) == 1
    assert rows[0]["memory_id"] == 7
    assert rows[0]["content"] == "一条测试记忆"      # **全文**，不是摘要


def test_路径上下文自动补():
    """审计未显式给 path 时自动补上下文路径（协议层用 memory_path 标记）。"""
    with logs.memory_path("mcp"):
        assert logs.current_memory_path() == "mcp"
        logs.audit_memory({"event": "read", "query": "x"})
    assert logs.current_memory_path() == "unknown"   # 退出上下文后复原
    assert logs.read_memory_audit()[0]["path"] == "mcp"


def test_显式path优先于上下文():
    with logs.memory_path("distill"):
        logs.audit_memory({"event": "write", "path": "http", "memory_id": 1})
    assert logs.read_memory_audit()[0]["path"] == "http"


def test_缺时间戳自动补():
    """漏 ts 会让查询显示 1970-01-01 —— 实测踩过（两处 read 审计都漏了），
    所以在 audit_memory 里兜底，而不是指望每个调用点都记得写。"""
    logs.audit_memory({"event": "read", "query": "x"})       # 故意不给 ts
    ts = logs.read_memory_audit()[0]["ts"]
    assert isinstance(ts, (int, float)) and ts > 1_700_000_000


def test_审计失败静默不炸主流程(monkeypatch):
    """审计是旁路：它坏了也必须让记忆操作本身成功（宁可少一条日志）。"""
    def boom(*_a, **_k):
        raise OSError("磁盘满了")
    monkeypatch.setattr(logs, "memory_audit_file", boom)
    logs.audit_memory({"event": "write"})            # 不抛异常即通过


# ---------------------------------------------------------------------------
# 抗误删：文件/目录被删后，下一次写入必须自动重建
#
# 为什么单独立一组：日志是**运维事实流**，"删了就永远不记了"比报错更危险
# ——审计静默失效，人还以为一切正常。实测（2026-09-21）：手工删掉 memory.log
# 后，MCP 写入自动重建并续记。
# ---------------------------------------------------------------------------

def test_各日志文件被删后自动重建():
    cases = {
        "mcp.log": lambda: logs.audit_mcp({"event": "x", "ts": 1}),
        "memory.log": lambda: logs.audit_memory({"event": "x", "ts": 1}),
        "wiki.log": lambda: logs.audit_wiki({"event": "x", "ts": 1}),
        "web.log": lambda: logs.record("抗误删测试"),
    }
    for name, write in cases.items():
        target = logs.log_dir() / name
        target.unlink(missing_ok=True)
        assert not target.exists(), name
        write()
        assert target.exists(), "%s 删除后未被自动重建" % name


def test_日志目录不存在时自动建多级目录(monkeypatch, tmp_path):
    """连目录一起删（甚至多级不存在）也要能自愈 —— mkdir(parents=True)。"""
    gone = tmp_path / "a" / "b" / "logs"
    monkeypatch.setattr(logs, "log_dir", lambda: gone)
    assert not gone.exists()
    logs.audit_memory({"event": "write", "ts": 1})
    assert (gone / "memory.log").exists()
    logs.audit_wiki({"event": "x", "ts": 1})
    logs.audit_mcp({"event": "x", "ts": 1})
    assert (gone / "wiki.log").exists() and (gone / "mcp.log").exists()


def test_任务日志子目录被删后自动重建(monkeypatch, tmp_path):
    """长任务输出落在 logs/tasks/ 子目录，同样要能自愈。"""
    d = tmp_path / "logs"
    monkeypatch.setattr(logs, "log_dir", lambda: d)
    logs.append_task_line("job-x", "第一行")
    p = d / "tasks" / "job-x.log"
    assert p.exists()
    p.unlink()
    logs.append_task_line("job-x", "删后第二行")
    assert p.exists() and "删后第二行" in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 数据层插桩：直写记忆（memory_save 路径）
# ---------------------------------------------------------------------------

def _mem_conn() -> sqlite3.Connection:
    from agentmemhub.distill import ensure_distill_schema
    conn = sqlite3.connect(":memory:")
    ensure_distill_schema(conn)
    return conn


def test_直写记忆落审计_含内容全文与协议层补充():
    from agentmemhub.distill import save_direct_memory
    conn = _mem_conn()
    r = save_direct_memory(conn, content="Agent 直写的记忆内容", type_="decision",
                           slice_key="mcp:abc123",
                           audit_extra={"path": "mcp", "importance": "high",
                                        "trace_id": "mcp_abc123"})
    assert r == "inserted"
    rows = logs.read_memory_audit()
    assert len(rows) == 1
    e = rows[0]
    assert e["event"] == "write" and e["path"] == "mcp"
    assert e["content"] == "Agent 直写的记忆内容"     # 全文
    assert e["memory_id"] == 1                      # 落表后能溯源
    assert e["type"] == "decision"
    assert e["result"] == "inserted"
    assert e["importance"] == "high"                # 协议层补充字段
    assert e["trace_id"] == "mcp_abc123"


def test_重复直写也留痕():
    """同内容二次写入记 result=duplicate —— "什么时候写过什么"要完整。"""
    from agentmemhub.distill import save_direct_memory
    conn = _mem_conn()
    save_direct_memory(conn, content="重复内容", audit_extra={"path": "mcp"})
    assert save_direct_memory(conn, content="重复内容",
                              audit_extra={"path": "mcp"}) == "duplicate"
    results = [r["result"] for r in logs.read_memory_audit()]
    assert results == ["inserted", "duplicate"]


def test_直写失败不记审计():
    """校验失败（非法 type）应在落表前抛错，不该留下"写过"的假记录。"""
    import pytest
    from agentmemhub.distill import save_direct_memory
    conn = _mem_conn()
    with pytest.raises(ValueError):
        save_direct_memory(conn, content="内容", type_="不存在的类型")
    assert logs.read_memory_audit() == []
