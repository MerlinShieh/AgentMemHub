# -*- coding: utf-8 -*-
"""wiki 增量更新触发器：写入时检测、满足规则才触发、触发后打标记。

设计原则（用户定下的）
======================
* **没有定时任务/调度线程**。检测完全挂在"写入记忆时"——每次蒸馏落库收尾
  做一次 align（毫秒级只读），按规则判定是否触发增量更新。
* **定期规则的语义是"时点档位"而非时钟触发**：设了 09:00/12:00/18:00，
  当天第一次发生在 09:00 之后的写入会补触发 09:00 档（以此类推）。
  一天不写入就一天不触发 —— wiki 本来就不需要比 RAG 更新。
* **触发后打标记**：update 成功才记录（时点档/每日首次/最后更新时间）；
  失败不记，下次写入自然重试。update 自身会把 manifest 刷新成新基线，
  align 归零 —— 即使标记丢失，也不会重复编译无变化的内容。
* **规则可编辑**：`set_overrides` 把运行时修改写进状态文件的 overrides 段
  （优先级：内置默认 < yaml < overrides），不碰 yaml 文件本身 —— 那里有
  用户注释，程序化改写会全部抹掉。

三条规则（命中任一且有脏数据即触发）：
  schedule          当前时间已越过某个今日未触发的时点档
  dirty_memories    脏记忆（新增+变更）≥ 阈值
  first_write_daily 每自然日第一次蒸馏写入（仅写入钩子，手动触发不算）
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from agentmemhub import wiki_manifest as wm

#: 进程内单飞行锁：update 是长任务，两个触发点撞上时后到者直接放弃
_RUN_LOCK = threading.Lock()

#: **跨进程**单飞行锁的文件（update 可能从 MCP 进程、CLI、面板、脚本被同时触发）。
#:
#: 为什么必须是跨进程的：`threading.Lock` 只在单进程内有效。2026-09-21 实测
#: MCP 的写入钩子（daemon 线程）与人工恢复脚本**并发**跑同一批产物 —— 日志里
#: 出现两份"两段式：先分组"（条数还是 101 / 102 两个版本），两者同时写
#: `out_l1_mimo` 与 manifest。
#:
#: 用**原子创建锁文件**（`O_CREAT|O_EXCL`）+ 内容写 PID/时间戳做跨进程互斥。
#:
#: 为什么不用 `msvcrt.locking` / `fcntl.flock`：实测在"追加模式打开 + 空文件先
#: 写一字节再锁"这个用法下，**子进程持锁时父进程仍能拿到锁**（跨进程互斥没生效，
#: 测试直接抓到了）。原子创建 + stale 判定语义明确、跨平台一致，代价是要自己
#: 处理"进程崩溃留下锁文件"，所以写 PID/时间戳并在失效时抢占。
_LOCK_FILE = Path("logs") / "wiki_update.lock"

#: 锁的最大持有时长（秒）：update 跑大会话可能近一小时，给足 3 小时。
#: 超过即视为失效锁 —— 崩溃残留不能永久挡住 wiki 更新。
_LOCK_MAX_AGE = 3 * 3600

#: 状态文件默认位置（logs/ 已忽略，状态不必入库）
STATE_FILE = Path("logs") / "wiki_trigger_state.json"


def _pid_alive(pid: int) -> bool:
    """判断进程是否存活。

    **刻意不依赖 psutil**：它不在依赖里（实测未安装），而"判断存活"一旦失败就
    会退化成"锁永久有效" —— 崩溃残留的锁会把 wiki 更新彻底挡住。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259            # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:                           # noqa: BLE001
            return True                             # 判断不了 → 保守认为存活
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:                               # noqa: BLE001
        return True


def _lock_is_stale() -> bool:
    """锁文件是否已失效：持锁进程不在了，或超过最大持有时长。"""
    try:
        parts = _LOCK_FILE.read_text(encoding="utf-8").split()
        pid, ts = int(parts[0]), float(parts[1])
    except Exception:                       # noqa: BLE001 —— 读不出来当失效
        return True
    if time.time() - ts > _LOCK_MAX_AGE:
        return True
    return not _pid_alive(pid)


def _take_lock() -> bool:
    """尝试原子创建锁文件；成功返回 True。"""
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True                         # 打不开（磁盘/权限）→ 退化为不锁
    try:
        os.write(fd, ("%d %.3f" % (os.getpid(), time.time())).encode("utf-8"))
    finally:
        os.close(fd)
    return True


@contextmanager
def _file_lock():
    """取得跨进程锁；拿不到时 yield False。

    锁文件异常（磁盘只读、目录不可建）时**退化为不锁** —— 宁可承担并发风险，
    也不能让 wiki 更新整体失效，更不能让异常炸掉调用方的写入流程。
    """
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:                       # noqa: BLE001
        yield True
        return
    got = _take_lock()
    if not got and _lock_is_stale():
        # 抢占失效锁：清掉后重试一次
        try:
            _LOCK_FILE.unlink()
        except OSError:
            pass
        got = _take_lock()
    try:
        yield got
    finally:
        if got:
            try:
                _LOCK_FILE.unlink()
            except OSError:
                pass


@contextmanager
def _single_flight():
    """单飞行：先取进程内锁，再取跨进程文件锁。yield False = 已有更新在跑。"""
    if not _RUN_LOCK.acquire(blocking=False):
        yield False
        return
    try:
        with _file_lock() as got:
            yield got
    finally:
        _RUN_LOCK.release()


# ---------------------------------------------------------------------------
# 配置：内置默认 < yaml < overrides（状态文件内）
# ---------------------------------------------------------------------------

def get_effective_config() -> dict[str, Any]:
    """触发规则的生效配置（合并三层）。"""
    from agentmemhub import config as hub_config
    base = dict(hub_config.config().wiki.get("update") or {})
    state = _load_state()
    return {**base, **(state.get("overrides") or {})}


def set_overrides(patch: dict[str, Any]) -> dict[str, Any]:
    """编辑触发规则（写状态文件的 overrides 段，优先级最高）。"""
    if not isinstance(patch, dict):
        raise ValueError("overrides 必须是对象")
    unknown = set(patch) - {"enabled", "schedule", "dirty_memories",
                            "first_write_daily"}
    if unknown:
        raise ValueError("未知字段：%s" % ", ".join(sorted(unknown)))
    if "schedule" in patch:
        patch["schedule"] = _validate_schedule(patch["schedule"])
    if "dirty_memories" in patch:
        if not isinstance(patch["dirty_memories"], int) or patch["dirty_memories"] < 1:
            raise ValueError("dirty_memories 必须是正整数")
    for b in ("enabled", "first_write_daily"):
        if b in patch and not isinstance(patch[b], bool):
            raise ValueError("%s 必须是布尔值" % b)
    state = _load_state()
    ov = state.get("overrides") or {}
    ov.update(patch)
    state["overrides"] = ov
    _save_state(state)
    return get_effective_config()


def _validate_schedule(schedule: Any) -> list[str]:
    """时点档必须是 HH:MM 列表（规范成零填充形式）；空列表 = 禁用定期档。"""
    if not isinstance(schedule, list):
        raise ValueError("schedule 必须是 HH:MM 列表（可为空 = 禁用定期档）")
    out = []
    for t in schedule:
        try:
            out.append(datetime.strptime(str(t).strip(), "%H:%M").strftime("%H:%M"))
        except ValueError:
            raise ValueError("时点格式错误：%r（应为 HH:MM）" % t)
    return out


# ---------------------------------------------------------------------------
# 状态：logs/wiki_trigger_state.json（触发标记的持久化）
# ---------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# 规则判定（纯函数，可单测）
# ---------------------------------------------------------------------------

def evaluate(*, now: datetime, dirty: int, has_dirty: bool, hook: str,
             cfg: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """按三条规则判定是否触发。

    hook: "write"（蒸馏写入钩子）| "manual"（手动触发）
    dirty: 脏记忆数（新增+变更）；has_dirty: align 判定存在任何待更新内容
    返回 {should_run, reasons, due_slots}
    """
    if not cfg.get("enabled", True):
        return {"should_run": False, "reasons": [], "due_slots": []}
    if not has_dirty:
        # 没有脏数据时任何规则都不触发 —— 触发的意义就是"把 wiki 追平"
        return {"should_run": False, "reasons": ["库未变化"], "due_slots": []}

    reasons: list[str] = []
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    fired = (state.get("fired") or {}).get(today) or []

    # ① 定期（时点档）：写入时补触发已越过且今日未触发的档位
    due_slots = [t for t in (cfg.get("schedule") or [])
                 if t <= hhmm and t not in fired]
    if due_slots:
        reasons.append("定期档位 %s 已到且未触发" % "/".join(due_slots))

    # ② 定量：脏记忆达到阈值
    threshold = cfg.get("dirty_memories")
    if threshold and dirty >= int(threshold):
        reasons.append("脏记忆 %d 条 ≥ 阈值 %d" % (dirty, int(threshold)))

    # ③ 每自然日首次写入（手动触发不算 —— 手动走 force，不该消耗每日额度）
    if cfg.get("first_write_daily", True) and hook == "write" \
            and state.get("first_write_date") != today:
        reasons.append("今日首次写入记忆")

    return {"should_run": bool(reasons), "reasons": reasons,
            "due_slots": due_slots}


def _mark_fired(state: dict[str, Any], now: datetime, due_slots: list[str],
                hook: str) -> None:
    """update **成功之后**才打标记 —— 失败不记，下次写入自然重试。"""
    today = now.strftime("%Y-%m-%d")
    if due_slots:
        fired = state.setdefault("fired", {})
        slots = set(fired.get(today) or [])
        slots.update(due_slots)
        fired[today] = sorted(slots)
        # 只留最近 7 天，防状态文件无限膨胀
        days = sorted(fired)
        for d in days[:-7]:
            fired.pop(d, None)
    if hook == "write":
        state["first_write_date"] = today
    state["last_update_at"] = now.isoformat(timespec="seconds")
    _save_state(state)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _targets() -> tuple[Path, Path, str]:
    """从配置解析 l1/l2 产出目录与索引库；未配置则抛可读错误。"""
    from agentmemhub import config as hub_config
    w = hub_config.config().wiki
    l1, l2 = (w.get("out_l1") or "").strip(), (w.get("out_l2") or "").strip()
    if not (l1 and l2):
        raise ValueError("未配置 wiki.out_l1 / wiki.out_l2 产出目录，触发器无法工作")
    from agentmemhub.wiki import _default_db
    return Path(l1), Path(l2), _default_db()


def on_memories_written() -> dict[str, Any]:
    """蒸馏写入钩子：检测 → 判定 → 满足则同步跑增量更新。

    全程旁路：任何异常都不影响蒸馏本身，只记日志。
    update 在当前线程同步执行（增量一般 1~2 分钟，蒸馏收尾本就在等报告）。
    """
    try:
        return _check_and_run(hook="write")
    except Exception as e:                      # noqa: BLE001
        try:
            from agentmemhub.logs import audit_wiki
            audit_wiki({"event": "trigger_error", "hook": "write",
                        "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
        except Exception:
            pass
        return {"checked": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}


def run_manual(*, force: bool = False) -> dict[str, Any]:
    """手动触发：force=True 绕过规则直接跑 update；否则仍按规则判定。"""
    return _check_and_run(hook="manual", force=force)


def _check_and_run(*, hook: str, force: bool = False) -> dict[str, Any]:
    # 单飞行（进程内锁 + 跨进程文件锁）：已有更新在跑就不重复触发。
    # update 自己会把 manifest 刷新成新基线，天然防重复编译。
    with _single_flight() as got:
        if not got:
            return {"checked": True, "should_run": False,
                    "message": "已有一次增量更新在执行中（可能来自其它进程），本次跳过"}
        return _run_locked(hook=hook, force=force)


def _run_locked(*, hook: str, force: bool = False) -> dict[str, Any]:
    """持锁后的判定与执行（主体）。"""
    from agentmemhub import wiki
    l1, l2, db = _targets()
    cfg = get_effective_config()

    a = wiki.align(l1, "l1", db)
    s = a.get("stages", {}).get("l1", {})
    dirty = (s.get("added_total") or 0) + (s.get("changed_total") or 0)
    state = _load_state()
    now = datetime.now()
    verdict = ({"should_run": True, "reasons": ["手动强制"],
                "due_slots": []} if force else
               evaluate(now=now, dirty=dirty, has_dirty=a["needs_recompile"],
                        hook=hook, cfg=cfg, state=state))
    out: dict[str, Any] = {
        "checked": True, "hook": hook, "should_run": verdict["should_run"],
        "reasons": verdict["reasons"], "dirty": dirty,
        "last_update_at": state.get("last_update_at"),
    }
    # 判定结果落 wiki.log —— 触发器此前**没有**这条日志，导致回查"某次为什么
    # 触发"只能靠 fired 记录 + 代码反推（实测踩过）；长任务必须能从日志回答
    # "为什么跑"，判定信息比执行记录更该留痕。
    try:
        from agentmemhub.logs import audit_wiki
        audit_wiki({"event": "trigger_check", "hook": hook,
                    "should_run": verdict["should_run"],
                    "reasons": verdict["reasons"],
                    "due_slots": verdict["due_slots"], "dirty": dirty,
                    "last_update_at": state.get("last_update_at")})
    except Exception:                       # noqa: BLE001 —— 审计旁路
        pass
    if not verdict["should_run"]:
        return out
    try:
        upd = wiki.update(l1_dir=l1, l2_dir=l2, db=db)
    except Exception as e:                  # noqa: BLE001 —— 失败如实返回，不打标记
        out["update"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}
        out["fired"] = False
        return out
    out["update"] = upd

    # **部分失败不算成功**：L1 有会话没编出来时不能打标记 —— 否则当日档位与
    # last_update_at 一起被消耗，而失败会话（已被剔除出新基线）要等到明天首次
    # 写入才有机会重试。实测 2026-09-21 就是这么把一次超时拖成静默丢失的：
    # update 只 catch 异常、不看返回值，failed=1 照样被当成成功。
    n_bad = (upd.get("l1") or {}).get("failed") or 0 if isinstance(upd, dict) else 0
    if n_bad:
        out["fired"] = False
        out["fired_note"] = ("L1 有 %d 个会话编译失败，不消耗触发额度"
                             "（下次写入自然重试）" % n_bad)
        try:
            from agentmemhub.logs import audit_wiki
            audit_wiki({"event": "trigger_fired_skipped", "hook": hook,
                        "l1_failed": n_bad, "reasons": verdict["reasons"]})
        except Exception:                   # noqa: BLE001 —— 审计旁路
            pass
        return out

    _mark_fired(_load_state(), now, verdict["due_slots"], hook)
    out["fired"] = True
    return out


def status() -> dict[str, Any]:
    """配置 + 状态快照（CLI / HTTP 共用）。"""
    state = _load_state()
    targets: dict[str, str] = {"l1": "", "l2": "", "db": ""}
    if _configured():
        l1, l2, db = _targets()
        targets = {"l1": str(l1), "l2": str(l2), "db": db}
    return {
        "config": get_effective_config(),
        "state": {
            "last_update_at": state.get("last_update_at"),
            "first_write_date": state.get("first_write_date"),
            "fired_today": (state.get("fired") or {}).get(
                datetime.now().strftime("%Y-%m-%d")) or [],
            "overrides": state.get("overrides") or {},
        },
        "state_file": str(STATE_FILE),
        "targets": targets,
    }


def _configured() -> bool:
    """wiki 是否可用：**总开关打开** + 两级产出目录都配了。

    `wiki.enabled` 此前**没有任何消费点** —— 关掉它照样触发（2026-09-22 配置
    审计发现的假开关）。现在它是真正的总开关：置 false 后触发器不再跑，
    `status()` 也会如实报告目标未就绪。
    """
    from agentmemhub import config as hub_config
    w = hub_config.config().wiki
    if not w.get("enabled", True):
        return False
    return bool((w.get("out_l1") or "").strip() and (w.get("out_l2") or "").strip())
