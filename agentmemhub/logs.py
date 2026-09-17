"""AgentMemHub 统一日志（<程序根>/logs/，按程序/接口分文件）。

结构：
    logs/
    ├── web.log        看板操作/接口（引擎启停、任务提交/结果摘要；内存环形缓冲供面板）
    ├── cli.log        CLI / 控制台操作记录（终端命令与结果摘要）
    ├── mcp.log        MCP 记忆调用审计（Agent 侧事实流，JSONL 只追加；见 audit_mcp）
    ├── engine.log     MemOS 引擎 daemon 输出（memos_daemon 日志）
    └── tasks/<id>.log 看板后台任务完整输出（逐行、带时间戳）
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from agentmemhub import config

_MAX_RING = 500
_LOCK = threading.Lock()
_FILE_LOCK = threading.Lock()      # 文件读写串行（多线程 append 竞态防护）
_RING: list[dict] = []


def log_dir() -> Path:
    """统一日志根目录：<程序根>/logs（跟随程序所在目录，不占系统盘数据目录）。"""
    from agentmemhub.config import PROJECT_ROOT
    return PROJECT_ROOT / "logs"


def _web_file() -> Path:
    return log_dir() / "web.log"


def record(msg: str, level: str = "info", actor: str = "web",
           dest: str = "web") -> dict:
    """写一条日志。

    dest=web：进内存环形缓冲（面板可读）+ logs/web.log（接口操作）；
    dest=cli：只写 logs/cli.log（终端/控制台操作，不进面板）。
    返回条目。
    """
    entry = {"ts": time.time(), "level": level, "actor": actor, "msg": msg}
    if dest == "web":
        with _LOCK:
            _RING.append(entry)
            if len(_RING) > _MAX_RING:
                del _RING[:-_MAX_RING]
        target = _web_file()
    else:
        target = log_dir() / "cli.log"
    try:
        with _FILE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return entry


def recent(limit: int = 100) -> list[dict]:
    """最近 N 条（新 -> 旧）。

    以 web.log 文件为准（持久化，面板重启后历史仍在）；内存环形缓冲仅兜底
    极端未落盘情况。
    """
    p = _web_file()
    entries: list[dict] = []
    if p.exists():
        try:
            with _FILE_LOCK:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for ln in lines[-limit:]:
                try:
                    entries.append(json.loads(ln))
                except Exception:
                    continue
        except Exception:
            pass
    if not entries:                             # 文件为空/不可读 → 回退内存缓冲
        with _LOCK:
            return list(_RING[-limit:])[::-1]
    return entries[::-1]                        # 新 -> 旧


# ---------------------------------------------------------------------------
# 任务完整输出落盘：logs/tasks/<job_id>.log（页面关掉/进程中断也可追溯）
# ---------------------------------------------------------------------------

def task_log_dir() -> Path:
    return log_dir() / "tasks"


def task_log_path(job_id: str) -> Path:
    return task_log_dir() / f"{job_id}.log"


# ---------------------------------------------------------------------------
# MCP 调用审计：logs/mcp.log（只追加的事实流，供溯源与查询）
#
# 为什么不复用 web.log：web.log 是"看板操作"的展示通道（还进面板内存环形
# 缓冲），而 MCP 调用是 Agent 侧的事实记录——读者、保留策略都不同。
# 审计只追加，不采样、不丢弃。
# ---------------------------------------------------------------------------

def mcp_audit_file() -> Path:
    return log_dir() / "mcp.log"


def audit_mcp(entry: dict) -> None:
    """追加一条 MCP 调用审计（JSONL）。

    写失败一律静默：审计是旁路，绝不能因为它而让记忆操作本身失败。
    """
    try:
        with _FILE_LOCK:
            p = mcp_audit_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_mcp_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读取 MCP 审计原始行（坏行跳过，不让一行损坏拖垮整个查询）。

    path 默认 `logs/mcp.log`，也接受日志**目录**（便于测试传临时目录）。
    limit>0 时只取末尾 N 行。解析放在生产侧而非各调用方：CLI 查询脚本、面板、
    测试都要读它，多处各写一份解析迟早漂移。
    """
    p = Path(path) if path else mcp_audit_file()
    if p.is_dir():
        p = p / "mcp.log"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit > 0:
        lines = lines[-limit:]
    out: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def append_task_line(job_id: str, line: str) -> Path:
    """把任务输出的一行追加到该 job 的完整日志文件（含时间戳前缀）。"""
    p = task_log_path(job_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
    except Exception:
        pass
    return p


def task_log_tail(job_id: str, limit: int = 40) -> str:
    """任务完整日志尾部（查看/面板展示用）。"""
    p = task_log_path(job_id)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-limit:])
    except Exception:
        return ""


def reset() -> None:
    """清空内存缓冲（测试用；不清文件）。"""
    with _LOCK:
        _RING.clear()