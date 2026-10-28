# -*- coding: utf-8 -*-
"""异步任务队列的**通用收割基础设施**（与具体通道无关）。

**它解决什么问题**：离线批处理（MiMo Batch API）是异步的 —— 提交后要等厂商
（实测 18 分钟 ~ 24 小时），完成了才回读、组装、落盘。项目的既有设计明确写着
"不做定时调度、没有后台定时任务、没有 daemon"，所以这里提供的**不是轮询循环**，
而是**事件驱动的惰性收割**：任何已有的触发点（MCP 启动 / 写入记忆 / 蒸馏收尾 /
CLI 动作）顺手调一次 `harvest()` 即可。

**本模块只说三件与通道无关的事**：

① **队列空 ⇒ 立即返回**（零成本、零副作用）。这是"实时更新不受影响"的依据 ——
   实时是同步的，队列永远是空的，收割对它恒为 no-op；
② **节流**：距上次检查不足 `interval_s` 就不再查（默认 300 秒，配置位
   `wiki.batch.check_interval_s`）。连续写入 10 条记忆不该查 10 次厂商；
③ **并发安全**：阈值只省流、**不保证唯一性**（两个钩子可能同时读到"上次是 6 分钟
   前"然后都去查），所以还要一把**跨进程锁**。两者都要。

至于"怎么查厂商、怎么回读"，由通道自己的 `Harvester` 注入 —— 本模块不认识
`batch_id`、`items`、`members` 这些离线字段。

另外留一份**检测流水**（`batch_checks.jsonl`，append-only）：每次检查记一行
（何时、跳过原因、结果、耗时、错误）。队列快照只有"现在"，没有"历史" ——
排障时"上次什么时候查的、为什么没动"只能靠它。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

#: 检测流水（与队列快照同目录，便于对照着看）
CHECKS_NAME = "batch_checks.jsonl"
#: 收割状态：节流时间戳 / 上次动作 / 连续错误 / 退避时刻
STATE_NAME = "harvest_state.json"
#: 跨进程单飞锁（收割专用；与 `wiki_triggers._file_lock()` 的更新锁**分开** ——
#: 收割只在"队列非空"时做事，而那时实时更新会被安全闸拦住，两者不重叠）
LOCK_NAME = ".harvest.lock"
#: 默认节流间隔（秒）
DEFAULT_INTERVAL_S = 300.0
#: 出错后的退避下限（秒）：平台抽风时别每 5 分钟戳一次
ERROR_BACKOFF_S = 1800.0
#: 锁的陈旧阈值（秒）：超过它且持锁进程已死 → 抢占
LOCK_STALE_S = 600.0


class Harvester(Protocol):
    """通道侧的收割器：告诉通用层"有没有在飞任务"以及"怎么推进它们"。"""

    def pending(self) -> list[Any]:
        """在飞任务列表。**空列表 = 空闲** —— 通用层据此零成本退出。"""
        ...

    def harvest(self, *, now: float) -> dict[str, Any]:
        """回读并推进（通道自己实现）。返回值会进检测流水。"""
        ...


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def check_interval_s(cfg: Optional[dict] = None) -> float:
    """节流间隔（秒）。配置位 `wiki.batch.check_interval_s`，默认 300。"""
    if cfg is None:
        try:
            from agentmemhub.config import config
            cfg = ((config().wiki or {}).get("batch") or {})
        except Exception:                   # noqa: BLE001 —— 配置读不到就用默认
            cfg = {}
    try:
        v = float((cfg or {}).get("check_interval_s", DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        v = DEFAULT_INTERVAL_S
    return max(0.0, v)


# ---------------------------------------------------------------------------
# 状态与流水（都是旁路：写不进去也不能影响业务）
# ---------------------------------------------------------------------------

def load_state(out: Path) -> dict[str, Any]:
    try:
        d = json.loads((Path(out) / STATE_NAME).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                       # noqa: BLE001
        return {}


def save_state(out: Path, st: dict[str, Any]) -> None:
    """原子写（`.tmp` + `replace`）—— 与队列快照同一套做法，断电不会写坏。"""
    p = Path(out) / STATE_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:                       # noqa: BLE001 —— 旁路
        pass


#: 检测流水的体积上限（字节）。超过就**只保留后半**，防止长期运行把它撑成
#: 一个谁都不敢读的大文件 —— 而 `read_checks()` 是**整份读进内存**的。
CHECKS_MAX_BYTES = 512 * 1024


def _trim_checks(p: Path) -> None:
    """流水超限时截断到**后半**（保留最近的记录，旁路、不抛）。"""
    try:
        if p.stat().st_size <= CHECKS_MAX_BYTES:
            return
        lines = p.read_text(encoding="utf-8").splitlines()
        keep = lines[-max(1, len(lines) // 2):]
        tmp = p.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(p)
    except Exception:                       # noqa: BLE001 —— 截断失败不影响追加
        pass


def append_check(out: Path, entry: dict[str, Any]) -> None:
    """追加一行检测流水（append-only，旁路，永不抛）。"""
    try:
        p = Path(out) / CHECKS_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": round(time.time(), 3), **entry}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        _trim_checks(p)
    except Exception:                       # noqa: BLE001
        pass


def read_checks(out: Path, limit: int = 50) -> list[dict[str, Any]]:
    """读最近若干条检测流水（排查用；只读，不抛）。"""
    try:
        lines = (Path(out) / CHECKS_NAME).read_text(encoding="utf-8").splitlines()
    except Exception:                       # noqa: BLE001
        return []
    rows: list[dict[str, Any]] = []
    for ln in lines[-max(1, limit):]:
        try:
            o = json.loads(ln)
            if isinstance(o, dict):
                rows.append(o)
        except Exception:                   # noqa: BLE001
            continue
    return rows


# ---------------------------------------------------------------------------
# 跨进程单飞锁
# ---------------------------------------------------------------------------

def _lock_holder(p: Path) -> tuple[int, float]:
    """读锁文件里的 (pid, ts)；读不到就当 (0, 0.0)（视为最陈旧）。"""
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
        return int(info.get("pid") or 0), float(info.get("ts") or 0.0)
    except Exception:                       # noqa: BLE001
        return 0, 0.0


def _try_lock(out: Path) -> Optional[Path]:
    """排它创建一个锁文件（`O_CREAT|O_EXCL` 是原子的）。成功返回路径。

    **陈旧判定只看时间戳，刻意不检查"进程是否存活"** —— 两个原因：

    ① **跨平台差异会咬人**：Windows 上 `os.kill(pid, 0)` 走的是
       `TerminateProcess(handle, 0)`，那是"**杀掉对方**"而不是"探测"。
       2026-10-01 实测：锁文件里记的 PID 正是 pytest 自己 → `_pid_alive(自己)`
       **把 pytest 进程杀了**，表现为"整条命令无输出、退出码 1"，排查成本很高。
    ② 收割本身有 5 分钟节流，锁最坏多留 `LOCK_STALE_S` 也只影响一轮 ——
       不值得为省这几百秒去碰进程句柄。
    """
    p = Path(out) / LOCK_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pid, ts = _lock_holder(p)
        if pid == os.getpid():
            return None                     # 自己持有（重入）：不抢、更不能当"死了"
        if (time.time() - ts) < LOCK_STALE_S:
            return None                     # 另一个进程正在收割 —— 这不是错误
        try:
            p.unlink()
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except Exception:                   # noqa: BLE001 —— 抢不到就算了，下次再来
            return None
    except Exception:                       # noqa: BLE001
        return None
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode())
    except Exception:                       # noqa: BLE001
        pass
    finally:
        os.close(fd)
    return p


def _unlock(p: Optional[Path]) -> None:
    if p is None:
        return
    try:
        Path(p).unlink()
    except Exception:                       # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def harvest(out: "Path | str", factory: Callable[[], Optional[Harvester]], *,
            now: Optional[float] = None, interval_s: Optional[float] = None,
            force: bool = False) -> dict[str, Any]:
    """收割一次。**永不抛**（收割是旁路，绝不能影响调用它的那条业务链）。

    返回 `{"action": ..., ...}`，`action` 取值：

    | action | 含义 | 代价 |
    |---|---|---|
    | `no_channel` | 通道没装配（如没配 api_key） | 0 |
    | `empty` | **队列空 ⇒ 立即返回**（实时通道恒走这条） | 一次本地文件读 |
    | `throttled` | 距上次检查不足 `interval_s` | 0（不查厂商） |
    | `locked` | 别的进程/线程正在收割（**不是错误**） | 0 |
    | `harvested` | 真推进了一次，详见 `result` | 取决于通道 |
    | `error` | 通道抛了（已退避，下次晚点再试） | 取决于通道 |

    `factory()` 期望返回通道收割器（`None` = 该通道不可用）。它被**惰性**调用，
    所以"没配离线凭据"时连队列都不读。
    """
    out = Path(out)
    now = time.time() if now is None else float(now)
    st = load_state(out)

    # ① 装配通道（没配就直接退，连队列都不读）
    try:
        h = factory()
    except Exception as e:                  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, str(e)[:160])
        return _log(out, now, "error", error=err, stage="factory")

    if h is None:
        return {"action": "no_channel"}

    # ② **队列空 ⇒ 立即返回**：零成本、零副作用。
    #    这条是"实时更新不受影响"的全部根据 —— 实时是同步的，队列恒空。
    try:
        pend = list(h.pending() or [])
    except Exception as e:                  # noqa: BLE001
        err = "%s: %s" % (type(e).__name__, str(e)[:160])
        return _log(out, now, "error", error=err, stage="pending")
    if not pend:
        st["last_checked_at"] = now
        st["last_action"] = "empty"
        st["consecutive_errors"] = 0
        st.pop("next_check_at", None)
        # **队列空 = 一切正常**，历史错误要清掉：否则它一直挂在状态文件里，
        # 排查时会被误导成"收割在失败"（2026-10-04 体检时我自己就被它骗了一次 ——
        # 实际是几小时前 MCP 路径的 ModuleNotFoundError 残留，而当时早已恢复正常）。
        st.pop("last_error", None)
        st.pop("last_seconds", None)
        st.pop("last_result", None)
        save_state(out, st)
        # **不写检测流水**：队列空是最常见的路径（CLI 每次调用都会走到），写进去只
        # 会把流水刷成噪音 —— 而"上次查过、当时队列空"已经由 `harvest_state.json`
        # 的 `last_action` 回答了。流水只留**有信息量**的动作。
        return {"action": "empty", "pending": 0}

    # ③ 退避：上一轮出错过 → 到点之前不再戳厂商
    nxt = float(st.get("next_check_at") or 0.0)
    if not force and nxt > now:
        return _log(out, now, "throttled", pending=len(pend),
                    reason="error_backoff", wait_s=round(nxt - now, 1))

    # ④ 节流：省 API 调用（阈值只省流，唯一性靠下面的锁）
    iv = check_interval_s() if interval_s is None else max(0.0, float(interval_s))
    last = float(st.get("last_checked_at") or 0.0)
    if not force and iv > 0 and (now - last) < iv:
        return _log(out, now, "throttled", pending=len(pend),
                    reason="interval", wait_s=round(iv - (now - last), 1))

    # ⑤ 跨进程单飞：抢不到说明别人正在收割 —— **这不是错误**，直接退
    lock = _try_lock(out)
    if lock is None:
        return _log(out, now, "locked", pending=len(pend))

    t0 = time.time()
    try:
        # ⑥ **抢到锁之后、开始遍历之前**就写下时间戳。不能等遍历完再写：
        #    否则"正在遍历"的窗口里，第二个钩子仍会通过节流判定（实测过的缺口）。
        st["last_checked_at"] = now
        st["last_action"] = "harvesting"
        save_state(out, st)
        try:
            res = h.harvest(now=now) or {}
            err = ""
        except Exception as e:              # noqa: BLE001 —— 通道抛错要如实记，别吞
            res, err = {}, "%s: %s" % (type(e).__name__, str(e)[:200])
    finally:
        _unlock(lock)

    # ⑦ 落状态（含退避）与检测流水
    st = load_state(out)
    st["last_action"] = "error" if err else "harvested"
    st["last_seconds"] = round(time.time() - t0, 2)
    st["last_result"] = {k: res.get(k) for k in
                         ("step", "consumed", "voided", "waited", "pages")
                         if k in res}
    if err:
        st["last_error"] = err
        st["consecutive_errors"] = int(st.get("consecutive_errors") or 0) + 1
        st["next_check_at"] = now + max(iv, ERROR_BACKOFF_S)
    else:
        st.pop("last_error", None)
        st.pop("next_check_at", None)
        st["consecutive_errors"] = 0
    save_state(out, st)
    return _log(out, now, "error" if err else "harvested", pending=len(pend),
                error=err, result=res, seconds=st["last_seconds"],
                stage="harvest")


def _log(out: Path, now: float, action: str, **fields: Any) -> dict[str, Any]:
    """记一行检测流水并返回结果（`ts` 用真实时刻，`at` 用传入的逻辑时刻）。"""
    row: dict[str, Any] = {"action": action, "at": round(now, 3), **fields}
    append_check(out, row)
    return row
