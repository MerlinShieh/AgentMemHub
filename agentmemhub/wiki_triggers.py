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
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agentmemhub import wiki_manifest as wm

#: 单飞行锁：update 是长任务，两个触发点撞上时后到者直接放弃
_RUN_LOCK = threading.Lock()

#: 状态文件默认位置（logs/ 已忽略，状态不必入库）
STATE_FILE = Path("logs") / "wiki_trigger_state.json"


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
    from agentmemhub import wiki
    l1, l2, db = _targets()
    cfg = get_effective_config()

    # 单飞行：已有更新在跑就不重复触发（update 自己也会刷新 manifest）
    if not _RUN_LOCK.acquire(blocking=False):
        return {"checked": True, "should_run": False,
                "message": "已有一次增量更新在执行中，本次跳过"}
    try:
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
        if not verdict["should_run"]:
            return out
        try:
            out["update"] = wiki.update(l1_dir=l1, l2_dir=l2, db=db)
        except Exception as e:                  # noqa: BLE001 —— 失败如实返回，不打标记
            out["update"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}
            return out
        _mark_fired(_load_state(), now, verdict["due_slots"], hook)
        return out
    finally:
        _RUN_LOCK.release()


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
    from agentmemhub import config as hub_config
    w = hub_config.config().wiki
    return bool((w.get("out_l1") or "").strip() and (w.get("out_l2") or "").strip())
