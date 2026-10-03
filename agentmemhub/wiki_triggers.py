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
#: 用**原子创建锁文件**（`O_CREAT|O_EXCL`）+ 内容写 PID/时间戳做跨进程互斥。
#:
#: 为什么不用 `msvcrt.locking` / `fcntl.flock`：实测在"追加模式打开 + 空文件先
#: 写一字节再锁"这个用法下，**子进程持锁时父进程仍能拿到锁**（跨进程互斥没生效，
#: 测试直接抓到了）。原子创建 + stale 判定语义明确、跨平台一致，代价是要自己
#: 处理"进程崩溃留下锁文件"，所以写 PID/时间戳并在失效时抢占。
#:
#: 锁的最大持有时长（秒）：update 跑大会话可能近一小时，给足 3 小时。
#: 超过即视为失效锁 —— 崩溃残留不能永久挡住 wiki 更新。
_LOCK_MAX_AGE = 3 * 3600


#: 触发状态文件与单飞锁的**真实路径**：惰性派生自 `logs.log_dir()`。
#:
#: 为什么必须惰性、且不能写成 `Path("logs")/…`（D-6，2026-10-01 根治）：
#: 相对路径**跟随进程 CWD** ——
#:   · MCP 进程与 CLI 进程的 CWD 可能不同 ⇒ 两边看到**不同的状态文件与锁**
#:     （单飞行失效、触发额度各算一套）；
#:   · 测试跑在项目根时更会**直接写进真实的生产状态文件**（实测：跑全量测试把
#:     `last_update_at` 改成了当天，排查时误以为"有一次更新跑过"，白查一轮产物）。
#: `logs.log_dir()` 会跟随 `logs` 配置（conftest 已把它指向临时目录，测试自动隔离）。
def state_path() -> Path:
    """触发状态文件的路径：**惰性派生自 `logs.log_dir()`**。

    为什么不能写成常量 `Path("logs")/…`（D-6，2026-10-01 根治）：相对路径**跟随进程
    CWD** —— MCP 与 CLI 的 CWD 可能不同（两边看到**不同的状态文件与锁**：单飞行失效、
    触发额度各算一套）；测试跑在项目根时更会**写进真实生产状态文件**（实测：跑全量
    测试把 `last_update_at` 改成当天，害我以为"真有一次更新跑过"，白查一轮产物）。

    ⚠️ 也**不用**模块级 `__getattr__`（PEP 562）取巧：`tests/test_console.py` 有一条
    "包内不得有未定义全局名"的静态检查，动态属性会让它（正确地）报错。
    """
    from agentmemhub.logs import log_dir
    return Path(log_dir()) / "wiki_trigger_state.json"


def lock_path() -> Path:
    """跨进程单飞锁的路径（与状态文件同源、同样惰性，理由见 `state_path()`）。"""
    from agentmemhub.logs import log_dir
    return Path(log_dir()) / "wiki_update.lock"


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
        parts = lock_path().read_text(encoding="utf-8").split()
        pid, ts = int(parts[0]), float(parts[1])
    except Exception:                       # noqa: BLE001 —— 读不出来当失效
        return True
    if time.time() - ts > _LOCK_MAX_AGE:
        return True
    return not _pid_alive(pid)


def _take_lock() -> bool:
    """尝试原子创建锁文件；成功返回 True。"""
    try:
        fd = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
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
        lock_path().parent.mkdir(parents=True, exist_ok=True)
    except Exception:                       # noqa: BLE001
        yield True
        return
    got = _take_lock()
    if not got and _lock_is_stale():
        # 抢占失效锁：清掉后重试一次
        try:
            lock_path().unlink()
        except OSError:
            pass
        got = _take_lock()
    try:
        yield got
    finally:
        if got:
            try:
                lock_path().unlink()
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
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=1),
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


def _audit(entry: dict) -> None:
    """写一条触发器日志（**全程旁路**：日志失败绝不影响触发流程）。"""
    try:
        from agentmemhub.logs import audit_wiki
        audit_wiki(entry)
    except Exception:                       # noqa: BLE001
        pass


def _mark_started(now: datetime, hook: str) -> None:
    """把「更新已开始」落到状态文件。

    为什么需要：`update()` 在写完 manifest 之后还有**页面层投影**（实测 4.5 分钟），
    期间状态文件仍是上一轮的标记 —— 外部看到 `run_end` 就去查状态，会以为
    "标记没打上、今天会重复触发"（2026-09-26 实测误报）。有了这个字段，
    `wiki --action triggers` 能直接显示"更新进行中（自 HH:MM:SS）"。
    """
    st = _load_state()
    st["update_started_at"] = now.isoformat(timespec="seconds")
    st["last_hook"] = hook
    _save_state(st)


def _mark_failed(err: str) -> None:
    """更新失败留痕（并清掉"进行中"）。

    失败原因此前只塞进返回值 —— 而 MCP 那条 daemon 路径会**丢弃返回值**，
    于是一次失败的触发完全无痕（看起来像"触发了但什么都没发生"）。
    """
    st = _load_state()
    st.pop("update_started_at", None)
    st["last_error_at"] = datetime.now().isoformat(timespec="seconds")
    st["last_error"] = str(err)[:300]
    _save_state(st)


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
    state.pop("update_started_at", None)        # 收工：清掉"进行中"
    state.pop("last_error", None)
    _save_state(state)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

#: 测试沙箱熔断开关（环境变量）。**进程级**，不随单个测试的 monkeypatch 撤销 ——
#: 这一条专治"**活过测试的线程**"：MCP 的写入钩子会起 daemon 线程
#: （`mcp_server._wiki_trigger_probe`），线程可能在本测试结束、monkeypatch 已被
#: 撤销之后才真正执行到 `_targets()` → 于是它拿到**真实配置**。
#:
#: 2026-09-26 实测事故（与 2026-09-19 那次同型）：`test_memory_save_passes_tags`
#: 调 MCP handler → 起线程 → 线程在 patch 撤销后执行，用**测试沙箱的 tmp 库**跑了
#: 一次"全量更新"，把**真实 `out_l1`** 当成产物目录：224 个 L1 页面被删到 25 个、
#: `manifest_l1.n_inputs` 从 1804 覆盖成 8、`pending` 一下涨到 200 条。
#: 靠 `monkeypatch` 的隔离挡不住它，所以必须有一道进程级、不撤销的熔断。
SANDBOX_ENV = "AGENTMEMHUB_TEST_SANDBOX"


def _targets() -> tuple[Path, Path, str]:
    """从配置解析 l1/l2 产出目录与索引库；未配置则抛可读错误。"""
    if os.environ.get(SANDBOX_ENV):
        raise RuntimeError("测试环境禁止解析真实 wiki 产出目录（%s 已置）"
                           % SANDBOX_ENV)
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
            # **跳过也要留痕**：并发时"本次没跑"此前完全查不到，事后只能看到
            # "明明有写入却没有 run_start"，无法区分"没触发"和"被跳过"。
            _audit({"event": "trigger_skip", "hook": hook,
                    "message": "已有一次增量更新在执行中（可能来自其它进程），本次跳过"})
            return {"checked": True, "should_run": False,
                    "message": "已有一次增量更新在执行中（可能来自其它进程），本次跳过"}
        return _run_locked(hook=hook, force=force)


def harvest_now() -> dict[str, Any]:
    """**纯收割**一次异步任务（供 MCP 启动 / CLI 入口等顺手调用）。**永不抛**。

    与 `_harvest_gate()` 的分工：那个是"更新前的闸门"（要回答"忙不忙"、据此决定
    要不要提交实时更新）；这个是"谁都能调的纯收割"，不关心结论。

    `_targets()` 自带**测试沙箱熔断**（`AGENTMEMHUB_TEST_SANDBOX`），所以测试里它
    会直接抛错并被这里吞掉 —— 不会真去查厂商。
    """
    try:
        from agentmemhub import task_queue, wiki_batch
        l1, l2, db = _targets()
        return task_queue.harvest(
            l2, lambda: wiki_batch.make_harvester(l1=l1, l2=l2, db=db))
    except Exception as e:                  # noqa: BLE001 —— 收割是旁路，永不抛
        return {"action": "error",
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}


def get_effective_batch_config() -> dict[str, Any]:
    """**离线触发器**的生效配置（`wiki.batch` 段，与实时的 `wiki.update` 并列）。

    与 `get_effective_config()` 的差别：**不吃状态文件的 overrides** —— 那层覆盖是
    实时规则的对外接口（`PUT /api/wiki/triggers`）的语义，离线配置改 yaml 即可
    （它本来就低频，不值得再开一层运行时可改的覆盖）。
    收割节流 `check_interval_s` 不参与触发判定，这里剔除。
    """
    from agentmemhub import config as hub_config
    seg = dict((hub_config.config().wiki or {}).get("batch") or {})
    seg.pop("check_interval_s", None)
    return seg


def _submit_offline(*, hook: str, dirty: int, now: datetime,
                    state: dict[str, Any], reasons: list[str],
                    due_slots: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """提交一轮**离线批次**。返回结果 dict；**通道不可用/失败时返回 `None`** ——
    调用方据此回落到实时（这就是"实时兜底"的落地方式）。
    """
    t0 = time.time()
    try:
        from agentmemhub import batch_ops
    except Exception as e:                  # noqa: BLE001 —— 装不上就当不可用
        _audit({"event": "trigger_update_error", "hook": hook, "channel": "offline",
                "error": "import batch_ops: %s" % str(e)[:200]})
        return None

    # 判定与开始都要留痕（与实时路径同一套事件，多一个 `channel` 字段）——
    # 排查时"这次走的是哪条通道"必须一眼可见。
    _audit({"event": "trigger_check", "hook": hook, "should_run": True,
            "reasons": reasons, "due_slots": [], "dirty": dirty,
            "channel": "offline", "last_update_at": state.get("last_update_at")})
    _audit({"event": "trigger_update_start", "hook": hook, "reasons": reasons,
            "dirty": dirty, "channel": "offline"})
    _mark_started(now, hook)
    try:
        res = batch_ops.run()
    except batch_ops.BatchOpsError as e:
        # **凭据 / 目录没配好 ⇒ 返回 None 让调用方回落到实时**（"兜底"的意义所在）
        _audit({"event": "trigger_update_error", "hook": hook, "channel": "offline",
                "error": str(e)[:300], "fallback": "realtime"})
        return None
    except Exception as e:                  # noqa: BLE001 —— 其它失败如实返回，不打标记
        err = "%s: %s" % (type(e).__name__, str(e)[:300])
        _audit({"event": "trigger_update_error", "hook": hook, "channel": "offline",
                "error": err, "seconds": round(time.time() - t0, 1)})
        _mark_failed(err)
        return {"checked": True, "hook": hook, "should_run": True,
                "reasons": reasons, "dirty": dirty, "channel": "offline",
                "updated": False, "error": err,
                "seconds": round(time.time() - t0, 1)}

    sub = ((res or {}).get("l2") or {}).get("offline_submitted") or {}
    # 提交成功才打标记 —— 与实时路径**共用同一套状态**。语义是"**一天一轮 wiki
    # 更新**，走哪条通道由优先级决定"；共用正是"离线优先"要的：离线跑过之后，
    # 当天实时的时点档与首写档就不再触发。
    _mark_fired(_load_state(), now, list(due_slots or []), hook)
    _audit({"event": "trigger_update_end", "hook": hook, "channel": "offline",
            "reasons": reasons, "dirty": dirty,
            "seconds": round(time.time() - t0, 1), "updated": False,
            "batch_id": sub.get("batch_id") or "",
            "stage": sub.get("stage") or "",
            "n_requests": sub.get("n_requests") or 0,
            "n_domains": sub.get("n_domains") or 0,
            "hints": (res or {}).get("hints") or []})
    return {"checked": True, "hook": hook, "should_run": True, "reasons": reasons,
            "dirty": dirty, "channel": "offline", "offline_submitted": sub,
            "seconds": round(time.time() - t0, 1)}


def _harvest_gate(*, l1: Path, l2: Path, db: str) -> dict[str, Any]:
    """先收割异步任务，并回答"队列里还有在飞的吗"。**旁路，永不抛**。

    这条闸解决一个具体的重复付费：离线批次**正在编译的域**，实时路径会**重新编译
    一遍**（结果一致，但白花一次实时的钱）。而队列空时（实时模式恒如此）
    `harvest()` 是**零成本立即返回** —— 所以它对实时**毫无影响**。这就是两条通道
    互不打架的全部机制，不需要额外的"模式"状态。
    """
    try:
        from agentmemhub import task_queue, wiki_batch
        r = task_queue.harvest(
            l2, lambda: wiki_batch.make_harvester(l1=l1, l2=l2, db=db))
        pend = int(r.get("pending") or 0)
        # `locked` 时 pending 未知（别的进程正在收割）→ 保守视为"忙"，不提交实时
        return {"action": r.get("action"), "pending": pend,
                "busy": pend > 0 or r.get("action") == "locked"}
    except Exception as e:                  # noqa: BLE001 —— 闸门坏了绝不能挡住实时
        return {"action": "error",
                "error": "%s: %s" % (type(e).__name__, str(e)[:160]), "busy": False}


def _run_locked(*, hook: str, force: bool = False) -> dict[str, Any]:
    """持锁后的判定与执行（主体）。"""
    from agentmemhub import wiki
    l1, l2, db = _targets()
    cfg = get_effective_config()            # 实时触发规则
    bcfg = get_effective_batch_config()     # 离线触发规则（判定时优先）

    # **先收割异步任务；队列非空就不提交实时更新**（详见 `_harvest_gate`）。
    # 放在 `align()` 之前还有个小好处：队列非空时省掉一次没用的对齐查询。
    gate = _harvest_gate(l1=l1, l2=l2, db=db)
    if gate.get("busy"):
        state = _load_state()
        reasons = ["离线批次在飞（%d 个任务）—— 只收割，不提交实时更新"
                   % gate.get("pending", 0)]
        # 判定要留痕（与其它"没跑"的路径一致：回答"为什么没触发"）
        try:
            from agentmemhub.logs import audit_wiki
            audit_wiki({"event": "trigger_check", "hook": hook,
                        "should_run": False, "reasons": reasons, "due_slots": [],
                        "dirty": 0, "last_update_at": state.get("last_update_at"),
                        "harvest": gate.get("action")})
        except Exception:                   # noqa: BLE001
            pass
        return {"checked": True, "hook": hook, "should_run": False,
                "reasons": reasons, "dirty": 0, "harvest": gate,
                "last_update_at": state.get("last_update_at")}

    a = wiki.align(l1, "l1", db)
    s = a.get("stages", {}).get("l1", {})
    dirty = (s.get("added_total") or 0) + (s.get("changed_total") or 0)
    state = _load_state()
    now = datetime.now()

    # ① **离线优先**：离线触发器开着就先判它，命中即提交离线批次并返回。
    #    两个都开 = "**离线优先、实时兜底**"；只开一个 = 纯某通道；都不开 = 全手动。
    #    注意 `--force` 只在**已启用的通道内**绕过规则 —— 它不会把本来关着的离线
    #    通道打开（否则一次手动强制就可能意外花掉离线批量的钱）。
    if bool(bcfg.get("enabled", False)):
        bverdict = ({"should_run": True, "reasons": ["手动强制"], "due_slots": []}
                    if force else
                    evaluate(now=now, dirty=dirty, has_dirty=a["needs_recompile"],
                             hook=hook, cfg=bcfg, state=state))
        if bverdict["should_run"]:
            res = _submit_offline(hook=hook, dirty=dirty, now=now, state=state,
                                  reasons=bverdict["reasons"],
                                  due_slots=bverdict.get("due_slots"))
            if res is not None:
                return res
            # 返回 None = 离线通道不可用（没配凭据 / 目录）→ **继续往下走实时**，
            # 这正是"实时兜底"。

    # ② 实时：离线没开 / 没命中 / 不可用时的路径（原行为，一字未改）
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

    # 判定通过 → **开始**要留痕：`trigger_check` 只说了"为什么该跑"，从这里到
    # 第一次 `run_start` 之间发生了什么（update 内部还有快照、align、投影）
    # 此前是断的。
    _audit({"event": "trigger_update_start", "hook": hook,
            "reasons": verdict["reasons"], "dirty": dirty})
    _mark_started(now, hook)
    t0 = time.time()
    try:
        upd = wiki.update(l1_dir=l1, l2_dir=l2, db=db)
    except Exception as e:                  # noqa: BLE001 —— 失败如实返回，不打标记
        err = "%s: %s" % (type(e).__name__, str(e)[:300])
        # **失败必须留痕**：此前只塞进返回值，而 MCP 那条 daemon 路径会**丢弃
        # 返回值** → 一次失败的触发完全无痕（看起来像"触发了但什么都没发生"）。
        _audit({"event": "trigger_update_error", "hook": hook, "error": err,
                "seconds": round(time.time() - t0, 1),
                "reasons": verdict["reasons"]})
        _mark_failed(err)
        out["update"] = {"error": err}
        out["fired"] = False
        return out
    out["update"] = upd

    # 成功也留**汇总**：想回答"这次更新做了什么"，此前只能去流水线日志里拼
    # （脏会话/域、L1 成败、L2 页数、投影耗时、提示都在返回值里，没人记）。
    g = upd if isinstance(upd, dict) else {}
    l1s, l2s = (g.get("l1") or {}), (g.get("l2") or {})
    _audit({"event": "trigger_update_end", "hook": hook,
            "seconds": round(time.time() - t0, 1),
            "updated": bool(g.get("updated")),
            "dirty_sessions": len(g.get("dirty_sessions") or []),
            "l1_recompiled": l1s.get("recompiled"),
            "l1_failed": l1s.get("failed"),
            "l2_domains": l2s.get("domains_recompiled"),
            "l2_pages": l2s.get("pages"),
            "l2_aborted": l2s.get("aborted") or "",
            "l2_skipped": l2s.get("skipped_domains") or [],
            "page_index_seconds": (g.get("page_index") or {}).get("seconds"),
            "hints": g.get("hints") or []})

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
    # 「更新进行中」要能被看到：`update()` 在写完 manifest 之后还有页面层投影
    # （实测 4.5 分钟），期间标记还没打上。没有这一项，外部看到 `run_end` 就查
    # 状态只会看到上一轮的标记，极易误判成"标记没打上/卡住了"（2026-09-26 实测）。
    started = state.get("update_started_at")
    in_progress: dict[str, Any] = {"running": False}
    if started:
        elapsed = None
        try:
            elapsed = round((datetime.now()
                             - datetime.fromisoformat(str(started))).total_seconds(), 1)
        except Exception:                   # noqa: BLE001 —— 格式坏就当算不出
            pass
        in_progress = {
            "running": True, "started_at": started, "hook": state.get("last_hook"),
            "elapsed_seconds": elapsed,
            "note": ("更新进行中（含最后的页面投影）；若该值长期不变，说明上一轮"
                     "异常退出，见 last_error"),
        }
    return {
        "config": get_effective_config(),
        # **离线触发器**的配置（与实时并列的那套）：排查"为什么没走离线"时要一眼
        # 看到 —— 它默认 `enabled: false`，而它与实时的组合决定判定顺序。
        "batch_config": get_effective_batch_config(),
        "state": {
            "last_update_at": state.get("last_update_at"),
            "first_write_date": state.get("first_write_date"),
            "fired_today": (state.get("fired") or {}).get(
                datetime.now().strftime("%Y-%m-%d")) or [],
            "overrides": state.get("overrides") or {},
            # 失败留痕（此前失败只塞进返回值，MCP 路径会丢掉 → 完全无痕）
            "last_error_at": state.get("last_error_at"),
            "last_error": state.get("last_error"),
        },
        "in_progress": in_progress,
        "state_file": str(state_path()),
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
