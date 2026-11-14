"""`agentmemhub memory-log` 输出测试：来源会话（写入会话）的展示。

为什么要专门测终端输出：CLI 是**没有面板时**排查记忆来源的唯一入口。
审计日志里已经有字段了，但若渲染时不带出来，用户仍然看不到 —— 这类
"数据有、界面没有"的缺口只能靠输出断言守住。

不测真实日志文件：`read_memory_audit` 换成替身，避免依赖本机历史。
"""
from __future__ import annotations

import types

from agentmemhub import cli, logs


def _args(**kw):
    """构造 argparse 命名空间（只含 cmd_memory_log 读取的字段）。"""
    base = dict(event=None, path=None, grep=None, limit=0, json=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _write_entry(**over) -> dict:
    entry = {
        "event": "write", "ts": 1794496132.0, "memory_id": 4603,
        "source": "mcp", "type": "decision", "content": "测试内容",
        "result": "inserted", "chars": 12, "path": "mcp",
    }
    entry.update(over)
    return entry


def test_memory_log_shows_source_session(monkeypatch, capsys):
    """有来源会话时要打出来，并标出它是「传的」还是「推断的」。"""
    monkeypatch.setattr(logs, "read_memory_audit", lambda: [
        _write_entry(source_session_id="dsh:session-abc",
                     source_session_origin="inferred")])
    assert cli.cmd_memory_log(_args()) == 0
    out = capsys.readouterr().out
    assert "dsh:session-abc" in out
    assert "inferred" in out


def test_memory_log_param_origin_is_distinguishable(monkeypatch, capsys):
    """`param`（调用方显式传入）必须与 `inferred`（服务端猜的）可区分 ——
    两者可信度不同，排查"来源记错了"时第一眼就要能看出是哪种。"""
    monkeypatch.setattr(logs, "read_memory_audit", lambda: [
        _write_entry(source_session_id="dsh:session-xyz",
                     source_session_origin="param")])
    cli.cmd_memory_log(_args())
    out = capsys.readouterr().out
    assert "（param）" in out


def test_memory_log_without_session_keeps_old_shape(monkeypatch, capsys):
    """历史记录（改造前写入、没有该字段）不能凭空多出一段。"""
    monkeypatch.setattr(logs, "read_memory_audit", lambda: [_write_entry()])
    cli.cmd_memory_log(_args())
    out = capsys.readouterr().out
    assert "[会话" not in out


def test_memory_log_truncates_long_session(monkeypatch, capsys):
    """超长会话 ID 要截断，否则一行被撑到几百字符、终端里没法读。"""
    long_sid = "dsh:session-" + "x" * 80
    monkeypatch.setattr(logs, "read_memory_audit",
                        lambda: [_write_entry(source_session_id=long_sid)])
    cli.cmd_memory_log(_args())
    out = capsys.readouterr().out
    assert "…" in out
    assert long_sid not in out


def test_memory_log_json_mode_carries_full_fields(monkeypatch, capsys):
    """`--json` 走原始结构，长会话 ID **不截断**（机读场景要完整值）。"""
    import json

    long_sid = "dsh:session-" + "y" * 80
    monkeypatch.setattr(logs, "read_memory_audit",
                        lambda: [_write_entry(source_session_id=long_sid)])
    cli.cmd_memory_log(_args(json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["source_session_id"] == long_sid
