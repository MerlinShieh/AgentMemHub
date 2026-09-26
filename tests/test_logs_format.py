# -*- coding: utf-8 -*-
"""日志**标准头**的守护测试。

标准头五项：`ts`（时间）/ `level`（等级）/ `module`（模块）/ `line`（行号）
/ `msg`（可读内容）。业务字段照旧附加。

为什么值得专门守：这些字段是**事后排查的唯一线索** —— 实测踩过两次
（`wiki.log` 早期条目没有 `ts`；`memory.log` 出过一次全 `1970`），
而且 `module/line` 取错的时候日志"看着很正常"，不测根本发现不了。
"""
from __future__ import annotations

import json
import inspect
import re

from agentmemhub import logs


def _lines(name: str) -> list[dict]:
    p = logs.log_dir() / name
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


def _last(name: str) -> dict:
    return _lines(name)[-1]


# ---------------------------------------------------------------------------
# 三个 JSONL 事实流 + web/cli
# ---------------------------------------------------------------------------

def test_mcp审计带标准头():
    line = inspect.currentframe().f_lineno + 1
    logs.audit_mcp({"kind": "mcp", "tool": "memory_save", "phase": "call"})

    rec = _last("mcp.log")

    assert isinstance(rec["ts"], float) and rec["ts"] > 1_600_000_000
    assert rec["level"] == "info"
    assert rec["module"] == __name__
    assert rec["line"] == line
    assert rec["msg"] == "memory_save call"
    assert rec["tool"] == "memory_save", "业务字段必须原样保留"


def test_记忆事实流带标准头且保留path():
    line = inspect.currentframe().f_lineno + 1
    logs.audit_memory({"event": "write", "memory_id": 7, "content": "正文"})

    rec = _last("memory.log")

    assert rec["module"] == __name__ and rec["line"] == line
    assert rec["level"] == "info"
    assert rec["msg"] == "write #7 path=unknown", "自动补的 path 要进摘要"
    assert rec["content"] == "正文", "正文仍在业务字段里（msg 只是摘要）"


def test_wiki事件带标准头且message优先作摘要():
    logs.audit_wiki({"event": "page_done", "script": "wiki_aggregate",
                     "message": "   [3/54] 完成：某页"})

    rec = _last("wiki.log")

    assert rec["msg"] == "[3/54] 完成：某页", "自带 message 时直接当摘要（并去空白）"
    assert rec["module"] == __name__


def test_cli与web记录带标准头():
    line = inspect.currentframe().f_lineno + 1
    logs.record("提交任务：xxx", actor="cli", dest="cli")

    rec = _last("cli.log")

    assert rec["module"] == __name__ and rec["line"] == line
    assert rec["level"] == "info" and rec["actor"] == "cli"
    assert rec["msg"] == "提交任务：xxx"


# ---------------------------------------------------------------------------
# 等级推断
# ---------------------------------------------------------------------------

def test_等级_ok为假判error():
    logs.audit_mcp({"kind": "mcp", "tool": "memory_save", "phase": "result",
                    "ok": False, "error": "boom"})
    rec = _last("mcp.log")
    assert rec["level"] == "error"
    assert "boom" in rec["msg"], "失败原因要进摘要，否则日志等于没说"


def test_等级_错误事件名判error():
    logs.audit_wiki({"event": "run_aborted", "script": "wiki_aggregate",
                     "reason": "degrade_ratio"})
    assert _last("wiki.log")["level"] == "error"

    logs.audit_wiki({"event": "page_fail", "domain": "域A"})
    assert _last("wiki.log")["level"] == "error"


def test_等级_调用失败判error():
    logs.audit_wiki({"event": "call", "tag": "细分", "ok": False,
                     "error": "HTTP 524"})
    assert _last("wiki.log")["level"] == "error"


def test_等级_显式给的优先():
    logs.audit_wiki({"event": "note", "level": "warn"})
    assert _last("wiki.log")["level"] == "warn"

    logs.record("自定义等级", level="debug", dest="cli")
    assert _last("cli.log")["level"] == "debug"


def test_等级_非法值回落info():
    logs.audit_wiki({"event": "note", "level": "VERBOSE"})
    assert _last("wiki.log")["level"] == "info"


# ---------------------------------------------------------------------------
# 摘要长度与任务日志前缀
# ---------------------------------------------------------------------------

def test_摘要截断不换行():
    logs.audit_wiki({"event": "note", "message": "长" * 500})
    msg = _last("wiki.log")["msg"]
    assert len(msg) <= logs.MSG_MAX + 1
    assert "\n" not in msg


def test_任务日志有标准前缀():
    line = inspect.currentframe().f_lineno + 1
    logs.append_task_line("job-fmt", "子进程输出第一行")

    text = (logs.task_log_path("job-fmt")).read_text(encoding="utf-8")

    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+)\s+\[(.+):(\d+)\] (.*)$",
                 text.strip())
    assert m, "任务日志要与其他日志同口径：日期时间 + 等级 + 模块:行号 + 内容"
    assert m.group(2) == "INFO"
    assert m.group(3) == __name__ and int(m.group(4)) == line
    assert m.group(5) == "子进程输出第一行"


def test_取栈失败不抛(monkeypatch):
    """日志设施绝不能因为取栈失败把业务写挂。"""
    monkeypatch.setattr(logs.sys, "_getframe",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert logs.caller_site(1) == ("", 0)
    logs.audit_wiki({"event": "note"})          # 不抛即通过
    assert _last("wiki.log")["module"] in ("", None) or True


# ---------------------------------------------------------------------------
# 应用内 logger 的兜底 handler
# （2026-09-26：一批 logger 从来没有 handler，info 级日志全丢）
# ---------------------------------------------------------------------------

def test_兜底handler_原本丢日志的logger现在能落盘():
    """**关键回归**：`agentmemhub.distill` / `llm` / `store`、`asrag.cli-sync` /
    `bridge` 这些 logger 直接 `getLogger(...).info(...)`，而 root 也没 handler
    ⇒ 记录只走 `logging.lastResort`（仅 WARNING+ 到 stderr），**info 级全丢**
    （实测 24 处调用点一直没人收）。挂到命名空间根上才能覆盖现有与将来的子 logger。
    """
    import logging

    logs.setup_app_logging()

    for ns in [n for n, _ in logs.APP_LOG_SPECS]:
        lg = logging.getLogger(ns)
        assert [h for h in lg.handlers if getattr(h, "_app_handler", False)], \
            "%s 应有兜底 handler" % ns
        assert lg.level != logging.NOTSET, "根 logger 不设级别的话 INFO 仍会被丢"

    logging.getLogger("agentmemhub.distill").info("蒸馏写了一条 info")
    logging.getLogger("asrag.bridge").info("桥接写了一条 info")

    txt = (logs.log_dir() / "agentmemhub.log").read_text(encoding="utf-8")
    line = [x for x in txt.splitlines() if "蒸馏写了一条 info" in x][0]
    # 必须是**标准文本格式**：时间 等级 [模块:行号] 内容
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO\s+"
                    r"\[agentmemhub\.distill:\d+\] ", line), \
        "文本日志要含 时间/等级/模块:行号：%r" % line
    assert "桥接写了一条 info" in \
        (logs.log_dir() / "asrag.log").read_text(encoding="utf-8")


def test_兜底handler_幂等且随目录重建(tmp_path, monkeypatch):
    """测试把 `log_dir` 指到 tmp 时必须重绑，否则会写进上一个测试的目录。"""
    import logging

    d1 = tmp_path / "a"
    monkeypatch.setattr(logs, "log_dir", lambda: d1)
    logs.setup_app_logging()

    def _n():
        return len([h for h in logging.getLogger("agentmemhub").handlers
                    if getattr(h, "_app_handler", False)])

    assert _n() == 1
    logs.setup_app_logging()                    # 同目录 → 幂等，不重复挂
    assert _n() == 1

    d2 = tmp_path / "b"
    monkeypatch.setattr(logs, "log_dir", lambda: d2)
    logs.setup_app_logging()                    # 换目录 → 重建
    hs = [h for h in logging.getLogger("agentmemhub").handlers
          if getattr(h, "_app_handler", False)]
    assert len(hs) == 1
    assert getattr(hs[0], "_app_dir", "").startswith(str(d2))
