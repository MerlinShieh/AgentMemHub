"""AgentMemHub 统一日志（<程序根>/logs/，按程序/接口分文件）。

结构：
    logs/
    ├── web.log        看板操作/接口（引擎启停、任务提交/结果摘要；内存环形缓冲供面板）
    ├── cli.log        CLI / 控制台操作记录（终端命令与结果摘要）
    ├── mcp.log        MCP 记忆调用审计（Agent 侧事实流，JSONL 只追加；见 audit_mcp）
    ├── memory.log     记忆操作事实流（数据层：哪条记忆被写/读/删，含内容；见 audit_memory）
    ├── wiki.log       LLM Wiki 编译事件（阶段/目标/token/成败/断点；见 audit_wiki）
    ├── engine.log     MemOS 引擎 daemon 输出（memos_daemon 日志）
    └── tasks/<id>.log 看板后台任务完整输出（逐行、带时间戳）

**为什么 wiki 要单独一个文件**：wiki 编译是长任务（实测第二级 39 分钟、246 次
LLM 调用），控制台输出一关就没了；而长任务必须能回答"跑到哪了 / 哪些失败了 /
花了多少 / 上次断在哪"。这与 mcp.log 的读者和保留策略都不同，所以不混用。
"""
from __future__ import annotations

import contextvars
import gzip
import json
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
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


# ---------------------------------------------------------------------------
# 滚动与归档（全局策略 + 单文件覆盖）
#
# 规则：
#   · **作用域**：全局 `logs.rotate` 对**所有**日志生效；`logs.files.<日志名>`
#     覆盖它——单文件那段为空（或整段没写）时自动回落全局。
#   · **滚动规则二选一**（不会同时命中）：配了 `max_mb`（>0）→ 按**大小**滚动，
#     **这是最高优先级**；没有大小规则但 `daily` 为真 → 按**自然日首次写入**
#     滚动（靠文件 mtime 的日期判定，无需额外状态文件）；两者都没有 → 不滚动。
#   · **归档**：`<日志目录>/<archive_dir>/<日志名>/<日志名>.<时间戳>`——先按
#     日志名分文件夹，归档文件放进去。
#   · `compress: false`（当前默认）→ 归档保留**原始文件数据**，直接搬走；
#     置 true 则 gzip（配置位预留）。
#   · 时间维度最低单位是**一个自然日（24 小时）**。
#
# 全程旁路：任何一步失败都静默跳过（下次写入再试）——日志设施绝不能因为磁盘/
# 权限问题把业务写挂。
# ---------------------------------------------------------------------------

def rotate_policy(name: str) -> dict:
    """某个日志的**生效**滚动策略：全局 `logs.rotate` < 单文件 `logs.files.<name>`。"""
    from agentmemhub import config as hub_config
    cfg = hub_config.config().logs or {}
    base = dict(cfg.get("rotate") or {})
    per = (cfg.get("files") or {}).get(name) or {}
    return {**base, **per}


def archive_dir(name: str = "") -> Path:
    """归档目录：`<日志目录>/<archive_dir>/<日志名>/`（按日志名分文件夹）。"""
    root = log_dir() / str(rotate_policy(name).get("archive_dir") or "archive")
    return (root / name) if name else root


def _maybe_archive(path: Path, name: str) -> None:
    """按生效策略滚动 + 归档 + 清理（任一步失败静默跳过）。"""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return
        pol = rotate_policy(name)
        st = path.stat()
        max_mb = float(pol.get("max_mb") or 0)
        if max_mb > 0:                      # ① 大小规则（最高优先级）
            rotate = st.st_size >= max_mb * 1024 * 1024
        elif pol.get("daily"):              # ② 自然日规则（二选一，不会同时生效）
            day = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
            rotate = day != datetime.now().strftime("%Y-%m-%d")
        else:
            rotate = False
        if not rotate:
            return
        dest_dir = archive_dir(name)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 时间戳精确到**毫秒**：秒级精度下，清理腾出的旧名字会被下一次滚动重用
        # （同一秒内多次滚动 + 清理后同名可复用），归档名会指向不同内容。
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        dest = dest_dir / ("%s.%s" % (name, stamp))
        n = 0
        while dest.exists():                # 同一秒内滚动两次
            n += 1
            dest = dest_dir / ("%s.%s_%d" % (name, stamp, n))
        if pol.get("compress"):
            with gzip.open(str(dest) + ".gz", "wb") as fo, \
                    open(path, "rb") as fi:
                shutil.copyfileobj(fi, fo)
            path.unlink()
        else:
            path.rename(dest)               # 暂不压缩：原始文件直接搬走
        _prune_archive(name, pol)
    except Exception:                       # noqa: BLE001
        pass


def _prune_archive(name: str, pol: dict) -> None:
    """按 keep_days 清理归档（超期删最旧；0 = 不限）。"""
    try:
        keep = float(pol.get("keep_days") or 0)
        if keep <= 0:
            return
        cutoff = time.time() - keep * 86400
        d = archive_dir(name)
        if not d.exists():
            return
        for f in d.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:                       # noqa: BLE001
        pass


def prune_task_logs() -> list[str]:
    """清理超期的**任务日志**（`logs/tasks/`），返回被删文件名。

    任务日志是"一次性任务输出"，每个 job 一个文件、不参与滚动；排查价值随时间
    衰减，所以按 `keep_days` **直接删**（与固定日志的"滚动→归档"路径不同）。
    """
    removed: list[str] = []
    try:
        keep = float(rotate_policy("tasks").get("keep_days") or 0)
        if keep <= 0:
            return []
        cutoff = time.time() - keep * 86400
        d = task_log_dir()
        if not d.exists():
            return []
        for f in d.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed.append(f.name)
    except Exception:                       # noqa: BLE001
        pass
    return removed


def _append_line(path: "Path | Any", line: str, *, name: str = "") -> None:
    """统一追加一行：建目录 → 滚动归档 → 追加（锁内串行）。

    所有日志写入点共用它——"抗误删 + 滚动策略"由此在所有日志上完全一致，
    将来新增日志只需给一个 `name`（对应 `logs.files.<name>` 的覆盖键）。

    `path` 可以是 Path，也可以是**返回 Path 的函数**——后者让"解析日志路径"
    这一步也落进 try 内：`log_dir()` 本身可能抛（磁盘/权限），若在调用处求值
    就会绕过容错把业务写挂。
    """
    try:
        p = Path(path()) if callable(path) else Path(path)
        with _FILE_LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            if name:
                _maybe_archive(p, name)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:                       # noqa: BLE001
        pass


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
        target, lname = _web_file, "web"
    else:
        target, lname = (lambda: log_dir() / "cli.log"), "cli"
    # 传函数而非求值结果：路径解析同样受 _append_line 的容错保护
    _append_line(target, json.dumps(entry, ensure_ascii=False) + "\n", name=lname)
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
    _append_line(mcp_audit_file,
                 json.dumps(entry, ensure_ascii=False) + "\n", name="mcp")


def read_mcp_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读取 MCP 审计原始行（坏行跳过，不让一行损坏拖垮整个查询）。

    path 默认 `logs/mcp.log`，也接受日志**目录**（便于测试传临时目录）。
    limit>0 时只取末尾 N 行。解析放在生产侧而非各调用方：CLI 查询脚本、面板、
    测试都要读它，多处各写一份解析迟早漂移。
    """
    return _read_jsonl(path, "mcp.log", limit)


def _read_jsonl(path: "Path | None", default_name: str, limit: int) -> list[dict]:
    """读 JSONL 日志（坏行跳过，不让一行损坏拖垮整个查询）。"""
    p = Path(path) if path else (log_dir() / default_name)
    if p.is_dir():
        p = p / default_name
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


# ---------------------------------------------------------------------------
# 记忆操作审计：logs/memory.log（**数据层**事实流）
#
# 为什么是**第三个**事实流（三者视角不同，混在一起会让"查一条记忆的来龙去脉"
# 变成考古）：
#   · mcp.log    —— **协议层**：谁调了什么工具、参数、耗时、成败
#   · memory.log —— **数据层**：哪条记忆被写/读/删、内容是什么、从哪条路径来（本段）
#   · wiki.log   —— **流水线层**：编译跑到哪、花了多少、断在哪
#
# 内容记**全文**（回答"当时到底写进去了什么"）：蒸馏记忆受 CONTENT_MAX=120 的
# 代码级硬截断（实测没有任何模型稳定遵守，故由代码兜底），而**直写记忆
# （memory_save）没有这层限制**——实测一条 698 字。所以膨胀靠**单文件上限 + 轮转**
# 兜底，而不是靠"记忆一定很短"的假设。
#
# 滚动/归档走**统一策略**（见上方「滚动与归档」段）：全局 `logs.rotate` 对所有
# 日志生效，`logs.files.memory` 可单独覆盖；归档进 `logs/archive/memory/`。
# ---------------------------------------------------------------------------

#: 当前记忆操作的**来源路径**（mcp / http / distill / cli）。
#: 用 contextvar 而不是层层传参：写入点在**数据层**（distill 的落表/投影），而
#: "这次操作从哪来"只有**协议层**（MCP / 面板 / CLI）知道——传参要改一长串函数
#: 签名，contextvar 让数据层直接读到当前路径。
_MEMORY_PATH: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agentmemhub_memory_path", default="unknown")


@contextmanager
def memory_path(path: str):
    """标记其内记忆操作的来源路径：`with logs.memory_path("mcp"): ...`。"""
    token = _MEMORY_PATH.set(path)
    try:
        yield
    finally:
        _MEMORY_PATH.reset(token)


def current_memory_path() -> str:
    """当前上下文里的记忆操作来源（未标记时为 `unknown`）。"""
    return _MEMORY_PATH.get()


def memory_audit_file() -> Path:
    return log_dir() / "memory.log"


def audit_memory(entry: dict) -> None:
    """追加一条记忆操作事实（JSONL）。

    写失败一律静默：审计是旁路，绝不能因为它而让记忆操作本身失败。
    调用方未显式给 `path` 时自动补当前上下文路径（见 memory_path）；
    未给 `ts` 时自动补时间戳（漏了会让查询显示 1970，实测踩过）。
    """
    if "path" not in entry:
        entry = {**entry, "path": current_memory_path()}
    if "ts" not in entry:
        entry = {**entry, "ts": round(time.time(), 3)}
    _append_line(memory_audit_file,
                 json.dumps(entry, ensure_ascii=False, default=str) + "\n",
                 name="memory")


def read_memory_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读取记忆操作事实流（坏行跳过；path 默认 `logs/memory.log`，也接受日志目录）。"""
    return _read_jsonl(path, "memory.log", limit)


# ---------------------------------------------------------------------------
# LLM Wiki 编译事件：logs/wiki.log（只追加的事实流）
#
# 为什么单独一个文件：wiki 编译是**长任务**——实测第二级跑了 39 分钟、246 次
# LLM 调用。控制台输出一关就没了，而长任务必须能回答四件事：跑到哪了、
# 哪些失败了、花了多少、上次断在哪。这三件都只能靠落盘。
#
# 与 mcp.log 的分工：mcp.log 记 Agent 的记忆调用（事实流），wiki.log 记编译
# 流水线自己的事件（阶段 / 目标 / token / 成败 / 断点）。读者与保留策略都不同。
# ---------------------------------------------------------------------------

def wiki_log_file() -> Path:
    return log_dir() / "wiki.log"


def audit_wiki(entry: dict) -> None:
    """追加一条 wiki 编译事件（JSONL）。

    写失败一律静默：审计是旁路，绝不能因为它让编译本身失败。
    """
    _append_line(wiki_log_file,
                 json.dumps(entry, ensure_ascii=False) + "\n", name="wiki")


def read_wiki_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读 wiki 编译事件（坏行跳过）。path 默认 `logs/wiki.log`，也接受日志目录。"""
    return _read_jsonl(path, "wiki.log", limit)


def append_task_line(job_id: str, line: str) -> Path:
    """把任务输出的一行追加到该 job 的完整日志文件（含时间戳前缀）。

    顺带清理超期任务日志（`prune_task_logs`）——长任务输出是"一次性"文件，
    不参与滚动，靠按天清理控制总量。
    """
    p = task_log_path(job_id)
    if not p.exists():
        prune_task_logs()
    _append_line(p, "[%s] %s\n" % (time.strftime("%H:%M:%S"), line))
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