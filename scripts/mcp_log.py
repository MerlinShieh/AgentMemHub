"""MCP 调用日志查询（`logs/mcp.log` 的读取端）。

日志由 MCP server 在 `_tools_call`（stdio 与 HTTP 的唯一分发点）自动写入，
每次调用两条：`phase=call`（谁·何时·调了什么·参数）与 `phase=result`
（成败·耗时·结果摘要）。**不依赖 Agent 记得写**——审计的价值全在完整性。

用法：
    .venv\\Scripts\\python.exe scripts/mcp_log.py                 # 最近 20 次调用
    .venv\\Scripts\\python.exe scripts/mcp_log.py --tool memory_save
    .venv\\Scripts\\python.exe scripts/mcp_log.py --failed        # 只看失败
    .venv\\Scripts\\python.exe scripts/mcp_log.py --grep 面板      # 搜参数/结果/意图
    .venv\\Scripts\\python.exe scripts/mcp_log.py --call <call_id> # 看某次调用全貌
    .venv\\Scripts\\python.exe scripts/mcp_log.py --json          # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmemhub.logs import mcp_audit_file, read_mcp_audit   # noqa: E402


def pair(entries: list[dict]) -> list[dict]:
    """按 call_id 把 call/result 配成一条记录（保持出现顺序）。

    只有 call 没有 result 的记录会保留并标记 `pending=True` —— 那代表"发起了
    却没有返回"（超时/进程被杀），正是审计要抓的情况，不能丢弃。
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for e in entries:
        cid = str(e.get("call_id") or "?")
        if cid not in by_id:
            by_id[cid] = {"call_id": cid, "tool": e.get("tool") or "?",
                          "agent": e.get("agent") or "-", "pending": True}
            order.append(cid)
        rec = by_id[cid]
        if e.get("phase") == "call":
            rec["ts"] = e.get("ts")
            rec["args"] = e.get("args") or {}
        else:
            rec["pending"] = False
            rec["ok"] = bool(e.get("ok"))
            rec["elapsed_ms"] = e.get("elapsed_ms")
            rec["error"] = e.get("error")
            rec["digest"] = e.get("digest") or {}
            rec["result_head"] = e.get("result_head") or ""
    return [by_id[c] for c in order]


def _match(rec: dict, kw: str) -> bool:
    blob = json.dumps(rec, ensure_ascii=False)
    return kw.lower() in blob.lower()


def render(records: list[dict], total: int) -> str:
    if not records:
        return ("（无匹配记录）\n"
                "提示：MCP 调用日志由服务端自动写入；若为空，说明本机还没有\n"
                "      Agent 通过 MCP 操作过记忆，或 MCP server 仍是旧进程。")
    out = [f"=== MCP 调用审计（显示 {len(records)} / 共 {total} 次）===", ""]
    for r in records:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts") or 0))
        if r.get("pending"):
            status, extra = "无响应", "← 只见 call 不见 result（超时/中断？）"
        elif r.get("ok"):
            status, extra = "OK", f"{r.get('elapsed_ms')}ms"
        else:
            status, extra = "失败", (r.get("error") or "")[:70]
        out.append(f"[{when}] {r['tool']:<15} {status:<6} {extra}")
        note = (r.get("args") or {}).get("note")
        if note:
            out.append(f"    意图: {note}")
        args = {k: v for k, v in (r.get("args") or {}).items() if k != "note"}
        if args:
            out.append(f"    参数: {json.dumps(args, ensure_ascii=False)[:160]}")
        dg = r.get("digest") or {}
        if dg:
            out.append(f"    标识: {json.dumps(dg, ensure_ascii=False)}")
        head = (r.get("result_head") or "").replace("\n", " ")
        if head:
            out.append(f"    结果: {head[:120]}")
        out.append(f"    call_id: {r['call_id']}")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP 调用日志查询")
    ap.add_argument("--limit", type=int, default=20, help="显示最近 N 次（默认 20）")
    ap.add_argument("--tool", default="", help="只看某工具，如 memory_save")
    ap.add_argument("--failed", action="store_true", help="只看失败/无响应的调用")
    ap.add_argument("--grep", default="", help="在参数/结果/意图中搜索关键词")
    ap.add_argument("--call", default="", help="查看指定 call_id 的完整记录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--path", default="", help="覆盖日志路径（默认 logs/mcp.log）")
    args = ap.parse_args()

    path = Path(args.path) if args.path else mcp_audit_file()
    records = pair(read_mcp_audit(path))

    if args.call:
        records = [r for r in records if r["call_id"].startswith(args.call)]
    if args.tool:
        records = [r for r in records if r["tool"] == args.tool]
    if args.failed:
        records = [r for r in records if r.get("pending") or not r.get("ok")]
    if args.grep:
        records = [r for r in records if _match(r, args.grep)]

    total = len(records)
    shown = records[-args.limit:]
    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
    else:
        print(f"日志文件: {path}")
        print(render(shown, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
