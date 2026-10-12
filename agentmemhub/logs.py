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
import os
import shutil
import sys
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


# ---------------------------------------------------------------------------
# 统一日志字段（所有日志共用的**标准头**）
#
# 标准头五项：`ts`（时间）/ `level`（等级）/ `module`（模块）/ `line`（行号）
# / `msg`（可读内容）；业务字段照旧附加。
#
# **为什么在写入口统一注入，而不是要求调用方写全**：写入点散落在 60+ 处
# （`mcp_server` / `web/app` / `distill` / `wiki_*` / `scripts/*`），靠"每个人都
# 记得写全"必然漂移 —— 实测 `wiki.log` 早期条目连 `ts` 都没有、`memory.log`
# 出过一次全 `1970`。所以约定：**调用方只写业务字段，标准头由这里补齐**；
# 调用方显式给了就以调用方为准（幂等，不覆盖）。
# ---------------------------------------------------------------------------

#: 允许的日志等级（小写）。与 Python logging 的 levelname 对齐。
LEVELS = ("debug", "info", "warn", "error")

#: `msg` 的长度上限：它是给人看的**单行摘要**，不是正文（正文在业务字段里）。
MSG_MAX = 200

#: 测试沙箱标记（进程级环境变量，会被子进程继承）。
#: 与 `wiki_triggers.SANDBOX_ENV` 同源同值 —— 那边拿它拦"解析真实产物目录"，
#: 这边拿它拦"往真实 logs/ 写日志"。两处都不依赖 monkeypatch，因为
#: **monkeypatch 挡不住活过测试的线程**（详见 `tests/conftest.py`）。
SANDBOX_ENV = "AGENTMEMHUB_TEST_SANDBOX"

#: 沙箱下日志目录的落点（由 `tests/conftest.py` 的 session 夹具置位）。
SANDBOX_LOG_DIR_ENV = "AGENTMEMHUB_TEST_LOG_DIR"


def caller_site(depth: int = 2) -> tuple[str, int]:
    """取调用点的 `(模块名, 行号)`。

    取不到返回 `("", 0)` —— **日志设施绝不能因为取栈失败把业务写挂**。
    """
    try:
        f = sys._getframe(depth)
        return str(f.f_globals.get("__name__") or ""), int(f.f_lineno)
    except Exception:                       # noqa: BLE001
        return "", 0


def _clip(s: Any) -> str:
    t = str(s or "").replace("\n", " ").strip()
    return t if len(t) <= MSG_MAX else t[:MSG_MAX] + "…"


def standardize(entry: dict, *, msg: str = "", level: str = "info",
                depth: int = 3) -> dict:
    """补齐标准头：`ts / level / module / line / msg`（显式给的不覆盖）。

    `depth` 是到**业务调用点**的栈深度：默认 3 =
    `standardize` ← `audit_xxx` ← 业务代码。
    """
    out = dict(entry)
    out.setdefault("ts", round(time.time(), 3))
    lv = str(entry.get("level") or level).lower()
    out["level"] = lv if lv in LEVELS else "info"
    if not entry.get("module") or not entry.get("line"):
        mod, ln = caller_site(depth)
        if not entry.get("module"):
            out["module"] = mod
        if not entry.get("line"):
            out["line"] = ln
    if msg and not out.get("msg"):
        out["msg"] = _clip(msg)
    return out


def _level_of(entry: dict, *, error_events: tuple[str, ...] = ()) -> str:
    """按内容推断等级：显式 `level` > `ok=False` > 事件名像错误 > info。"""
    if str(entry.get("level") or "").lower() in LEVELS:
        return str(entry["level"]).lower()
    if entry.get("ok") is False or entry.get("error"):
        return "error"
    ev = str(entry.get("event") or "")
    if ev in error_events or ev.endswith("_error") or ev.endswith("_fail"):
        return "error"
    return "info"



def sandbox_log_dir() -> Path | None:
    """测试沙箱下日志目录的落点；不在沙箱（或没配）时返回 `None`。

    抽成独立函数是为了**可测**：`conftest` 的 `audit_dir` 夹具会把
    `logs.log_dir` 整个 monkeypatch 掉，测试里根本调不到原函数 —— 而要验的
    恰恰是"**没有 monkeypatch 时**（= 活过测试的线程/子进程）会不会落到生产目录"。
    """
    if os.environ.get(SANDBOX_ENV) == "1":
        override = os.environ.get(SANDBOX_LOG_DIR_ENV) or ""
        if override:
            return Path(override)
    return None


def log_dir() -> Path:
    """统一日志根目录：<程序根>/logs（跟随程序所在目录，不占系统盘数据目录）。

    **测试沙箱**下改走 `AGENTMEMHUB_TEST_LOG_DIR`：`conftest` 的 `audit_dir`
    夹具只对**当前测试**生效（monkeypatch），而活过测试的线程/子进程拿不到它
    —— 实测生产 `logs/wiki.log` 里混进了 140 条测试错误，它们正是被沙箱熔断
    （`wiki_triggers` 的产物闸）拦下后、回头写日志留痕的那批。
    环境变量是**进程级**的、会被子进程继承，与那道熔断同源，逃逸路径也躲不开。
    """
    override = sandbox_log_dir()
    if override is not None:
        return override
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


#: 应用内 logger 的**兜底 handler** 挂点：命名空间根 → 日志文件。
#:
#: 为什么挂根上而不是逐个登记：`agentmemhub.distill` / `llm` / `store`、
#: `asrag.cli-sync` / `bridge` 这些 logger **从来没有 handler**（模块里直接
#: `logging.getLogger(...)` 就 `.info(...)`，而 root 也没有 handler）⇒ 记录只走
#: `logging.lastResort`（仅 WARNING+ 到 stderr），**info 级全丢**（实测 24 处调用点
#: 的日志一直没人收）。逐名登记挡不住"以后新增模块"，挂在命名空间根上才能
#: 一次覆盖全部子 logger。
#:
#: 不会与 `rag.logkit` 重复：`get_logger()` 给 `asrag.<组件>` 挂了专属文件并设了
#: `propagate = False`，所以那批记录只进自己的文件，不会再到 `asrag.log`。
APP_LOG_SPECS = (("agentmemhub", "agentmemhub.log"), ("asrag", "asrag.log"))

#: 文本形态的标准头（与 `rag/logkit.py` 同口径，两处必须一致）。
_TEXT_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s:%(lineno)d] %(message)s"


def setup_app_logging(level: int | None = None) -> list[str]:
    """给应用内的 logger 挂标准格式的文件 handler（幂等，可重入）。

    绑定目录变了（测试把 `log_dir` 指到 tmp）会**重建** handler —— 与
    `logkit.get_logger` 同一套"同组件同目录才幂等"的做法。
    返回实际挂上的文件路径列表；任何一步失败都静默（日志设施绝不能把业务写挂）。
    """
    import logging
    import logging.handlers
    out: list[str] = []
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
    except Exception:                       # noqa: BLE001
        return out
    for ns, fname in APP_LOG_SPECS:
        try:
            lg = logging.getLogger(ns)
            target = d / fname
            if any(getattr(h, "_app_handler", False)
                   and getattr(h, "_app_dir", None) == str(target)
                   for h in lg.handlers):
                out.append(str(target))     # 已绑同一目录 → 幂等
                continue
            for h in list(lg.handlers):     # 目录变了（测试隔离）→ 重建
                if getattr(h, "_app_handler", False):
                    lg.removeHandler(h)
                    try:
                        h.close()
                    except Exception:       # noqa: BLE001
                        pass
            fh = logging.handlers.RotatingFileHandler(
                target, maxBytes=5 * 1024 * 1024, backupCount=3,
                encoding="utf-8")
            fh.setFormatter(logging.Formatter(_TEXT_FORMAT))
            fh._app_handler = True          # type: ignore[attr-defined]
            fh._app_dir = str(target)       # type: ignore[attr-defined]
            lg.addHandler(fh)
            if lg.level == logging.NOTSET:
                lg.setLevel(level if level is not None else logging.INFO)
            out.append(str(target))
        except Exception:                   # noqa: BLE001
            continue
    return out


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
        # 顺带把应用内 logger 的兜底 handler 挂上（幂等、失败静默）——放在这里
        # 是因为它是**所有**日志的唯一漏斗：任何会写日志的进程都会走到，不用去
        # 逐个入口记得调 `setup_app_logging()`。
        setup_app_logging()
    except Exception:                       # noqa: BLE001
        pass


def record(msg: str, level: str = "info", actor: str = "web",
           dest: str = "web") -> dict:
    """写一条日志。

    dest=web：进内存环形缓冲（面板可读）+ logs/web.log（接口操作）；
    dest=cli：只写 logs/cli.log（终端/控制台操作，不进面板）。
    返回条目（含标准头 ts/level/module/line/msg，调用点由栈自动取）。
    """
    # depth=3 与 audit_* 同口径：caller_site(0) ← standardize(1) ← 本函数(2) ← 业务(3)
    # `msg` 必须经 `msg=` 参数传：直接塞进 entry 会让 `standardize` 以为"摘要已经
    # 给过了"从而**跳过 `_clip`** —— 超长、多行文本原样落盘（实测 `web.log` 里
    # 躺着 180 字且带换行的任务消息）。所有事实流的摘要口径必须一致：单行、≤ MSG_MAX。
    entry = standardize({"ts": time.time(), "level": level, "actor": actor},
                        msg=msg, depth=3)
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


def _mcp_msg(e: dict) -> str:
    """把一条 MCP 审计渲染成单行可读摘要（`msg` 字段）。"""
    bits = [str(e.get("tool") or e.get("event") or "mcp")]
    if e.get("phase"):
        bits.append(str(e["phase"]))
    if e.get("ok") is False:
        bits.append("失败")
        if e.get("error"):
            bits.append(str(e["error"])[:120])
    elif e.get("ok") is True:
        bits.append("OK" + (" %sms" % e["elapsed_ms"]
                            if e.get("elapsed_ms") is not None else ""))
    return " ".join(bits)


def audit_mcp(entry: dict) -> None:
    """追加一条 MCP 调用审计（JSONL，带标准头 ts/level/module/line/msg）。

    写失败一律静默：审计是旁路，绝不能因为它而让记忆操作本身失败。
    """
    e = standardize(entry, msg=entry.get("msg") or _mcp_msg(entry),
                    level=_level_of(entry), depth=3)
    _append_line(mcp_audit_file,
                 json.dumps(e, ensure_ascii=False) + "\n", name="mcp")


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


def _memory_msg(e: dict) -> str:
    """把一条记忆操作渲染成单行可读摘要（正文仍在业务字段里，这里只给摘要）。"""
    bits = [str(e.get("event") or "memory")]
    if e.get("memory_id") is not None:
        bits.append("#%s" % e["memory_id"])
    for k in ("path", "source", "conversation_id", "type", "status"):
        if e.get(k):
            bits.append("%s=%s" % (k, e[k]))
    if e.get("query"):
        bits.append("q=%s" % str(e["query"])[:80])
    return " ".join(bits)


def audit_memory(entry: dict) -> None:
    """追加一条记忆操作事实（JSONL，带标准头 ts/level/module/line/msg）。

    写失败一律静默：审计是旁路，绝不能因为它而让记忆操作本身失败。
    调用方未显式给 `path` 时自动补当前上下文路径（见 memory_path）；
    标准头（ts/level/module/line/msg）由 `standardize` 统一补齐。
    """
    if "path" not in entry:
        entry = {**entry, "path": current_memory_path()}
    e = standardize(entry, msg=entry.get("msg") or _memory_msg(entry),
                    level=_level_of(entry), depth=3)
    _append_line(memory_audit_file,
                 json.dumps(e, ensure_ascii=False, default=str) + "\n",
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


def _wiki_msg(e: dict) -> str:
    """把一条 wiki 事件渲染成单行可读摘要。

    写作进度这类事件自带 `message`，直接用它；否则按 script/stage/target 拼。
    """
    if e.get("message"):
        return str(e["message"])
    bits = [str(e.get("event") or "wiki")]
    for k in ("script", "stage", "target", "domain", "name", "tag"):
        if e.get(k):
            bits.append(str(e[k]).strip())
    if e.get("error"):
        bits.append("error=%s" % str(e["error"])[:120])
    return " ".join(bits)


#: wiki 事件里天然属于"出错"的事件名（`ok=False` 之外还要认这些）
_WIKI_ERROR_EVENTS = ("page_fail", "run_aborted", "domain_write_skipped")


def audit_wiki(entry: dict, *, depth: int = 3) -> None:
    """追加一条 wiki 编译事件（JSONL，带标准头 ts/level/module/line/msg）。

    写失败一律静默：审计是旁路，绝不能因为它让编译本身失败。

    `depth=3` 对应"业务代码**直接**调 `audit_wiki(...)`"；若中间还隔了一层包装
    （如 `wiki_triggers._audit()`），调用方必须**自己 +1** —— 否则 `line` 记的是
    包装函数内部那一行，该流所有事件的行号恒等，字段等于作废（2026-10-04 实测）。
    """
    e = standardize(entry, msg=entry.get("msg") or _wiki_msg(entry),
                    level=_level_of(entry, error_events=_WIKI_ERROR_EVENTS),
                    depth=depth)
    _append_line(wiki_log_file,
                 json.dumps(e, ensure_ascii=False) + "\n", name="wiki")


def read_wiki_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读 wiki 编译事件（坏行跳过）。path 默认 `logs/wiki.log`，也接受日志目录。"""
    return _read_jsonl(path, "wiki.log", limit)


# ---------------------------------------------------------------------------
# 通知事实流（logs/notify.log）
#
# 通知本身已落 SQLite（`agentmemhub/notify.py`，可查询、有已读态）；这里留的是
# **事实流水**：谁在什么时候发了什么通知、落库成没成功。两者职责不同，都要有。
# ---------------------------------------------------------------------------

def notify_log_file() -> Path:
    return log_dir() / "notify.log"


def _notify_msg(e: dict) -> str:
    return "%s [%s] %s" % (e.get("level", ""), e.get("category", ""),
                           e.get("title", ""))


#: 通知的等级用 `warning`（更标准的写法），日志流统一用 `warn` —— 在入口映射一次，
#: 免得两条流各写各的、grep 时漏。
_NOTIFY_LEVEL_MAP = {"warning": "warn"}


def audit_notify(entry: dict) -> None:
    """追加一条通知事件（JSONL，带标准头 ts/level/module/line/msg）。

    写失败一律静默：通知是旁路，绝不能因为它让业务失败。

    ⚠️ 等级必须在**进 `standardize` 之前**改写：`standardize` 的规则是
    "`entry["level"]` 优先于传参的 `level`"（`lv = entry.get("level") or level`），
    而通知用的 `warning` 不在日志流的 `LEVELS`（`debug/info/warn/error`）里 ——
    不先改写就会被回落成 `info`，白白丢掉"这是警告"这个信息（实测踩过）。
    """
    src = dict(entry)
    lv = str(src.get("level") or "info").strip().lower()
    src["level"] = _NOTIFY_LEVEL_MAP.get(lv, lv)
    e = standardize(src, msg=entry.get("msg") or _notify_msg(entry), depth=3)
    _append_line(notify_log_file,
                 json.dumps(e, ensure_ascii=False) + "\n", name="notify")


def read_notify_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读通知事件（坏行跳过）。path 默认 `logs/notify.log`，也接受日志目录。"""
    return _read_jsonl(path, "notify.log", limit)


# ---------------------------------------------------------------------------
# 投喂事实流（logs/feed.log）
#
# 投喂是长流程（取回 → 转译 → 发布），且**每一步都可能部分失败**；落库（materials.db）
# 记的是"当前状态"，事实流记的是"过程"——两者互补：排障要过程，查询要状态。
# ---------------------------------------------------------------------------

def feed_log_file() -> Path:
    return log_dir() / "feed.log"


def _feed_msg(e: dict) -> str:
    parts = [str(e.get("event") or ""), str(e.get("kind") or "")]
    if e.get("doc_id"):
        parts.append("doc=%s" % e["doc_id"])
    if e.get("title"):
        parts.append(str(e["title"]))
    if e.get("error"):
        parts.append("error=%s" % str(e["error"])[:120])
    return " ".join(p for p in parts if p)


#: 投喂流里天然属于"出错"的事件名（除 `ok=False` / `error` 之外的判据）
_FEED_ERROR_EVENTS = ("feed_failed",)


def audit_feed(entry: dict, *, depth: int = 3) -> None:
    """追加一条投喂事件（JSONL，带标准头 ts/level/module/line/msg）。

    写失败一律静默：投喂是长流程，审计绝不能反过来把它弄失败。
    `depth=3` 对应业务代码**直接**调用；隔了包装层的话调用方要自己 +1
    （与 `audit_wiki()` 同一约定，否则 `line` 恒等、字段作废）。
    """
    e = standardize(entry, msg=entry.get("msg") or _feed_msg(entry),
                    level=_level_of(entry, error_events=_FEED_ERROR_EVENTS),
                    depth=depth)
    _append_line(feed_log_file(),
                 json.dumps(e, ensure_ascii=False) + "\n", name="feed")


def read_feed_audit(path: "Path | None" = None, limit: int = 0) -> list[dict]:
    """读投喂事件（坏行跳过）。path 默认 `logs/feed.log`，也接受日志目录。"""
    return _read_jsonl(path, "feed.log", limit)


def append_task_line(job_id: str, line: str) -> Path:
    """把任务输出的一行追加到该 job 的完整日志文件（**统一标准前缀**）。

    以前只有 `[HH:MM:SS]`（没有日期、等级、模块、行号），跨天排查时无法定位是哪天；
    现在与其他日志同口径：`YYYY-MM-DD HH:MM:SS LEVEL [module:line] 内容`。
    任务输出本身是逐行透传的第三方/子进程文本，等级固定 `INFO`（不是日志库里
    那种"带等级的事件"），模块/行号取**调用点**（即哪个组件在转写这些行）。

    顺带清理超期任务日志（`prune_task_logs`）——长任务输出是"一次性"文件，
    不参与滚动，靠按天清理控制总量。
    """
    p = task_log_path(job_id)
    if not p.exists():
        prune_task_logs()
    mod, ln = caller_site(2)          # caller_site ← 本函数 ← 调用方
    _append_line(p, "%s %-5s [%s:%d] %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), "INFO", mod or "task", ln, line))
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