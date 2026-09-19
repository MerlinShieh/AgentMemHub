# -*- coding: utf-8 -*-
"""wiki 编译清单（manifest）：wiki 与 RAG 库对齐的锚点。

背景
====
wiki 是一次性离线编译的产物，而 RAG 库是活的：蒸馏继续写入、内容被修复截断、
会话被删除、投影被重算。此前 wiki 只知道自己"何时编译"（页面里的 `compiled_at`），
不知道自己"**基于哪些记忆的哪些版本**"建成 —— 于是库发生了什么变更，wiki 完全
无感知（实测：编译后真实库发生了 3 类变更、688 条内容改写、163 条输入恢复，
wiki 全部不知道，其中 163 条有效记忆至今没进 wiki）。

manifest 解决的就是这个：每次编译收尾写一份**输入快照**
（记忆 id → content_hash + 会话归属）。之后任何时刻做一次 diff 就能精确回答：

  · added    —— 库里新出现的输入记忆（该进 wiki 而没进的）
  · removed  —— 编译时是输入、现在不是了（被删除，或转成 merged/duplicate）
  · changed  —— 两边都有但 content_hash 不同（内容被改写）
  · 脏会话/脏主题 —— 上述变更落到了哪些会话（L1）/ 哪些主题域（L2）

设计取舍
========
· 口径统一为"全库 `status IN ('new','similar')`"—— L1 的选择口径本来如此；
  L2 的输入经 L1 页面传递，潜在可用集合也是同一批，因此两级 manifest 结构一致，
  diff 逻辑只写一份。
· 指纹现算 md5(content)，不用库里的 content_hash 字段 —— 表里没有 updated_at，
  而 content_hash 只在蒸馏写入时生成，此后绕过蒸馏链路的直接改库（实测：
  overlong 截断修复 UPDATE 了 688 条 content，hash 未重算）会让它失真漏报；
  自算指纹才保证"内容一变就能检测到"。
· diff 的 removed 再细分为 missing（真被删了）与 reclassified（状态迁移）——
  两者的处理方式不同：前者要把死引用从 wiki 里清掉，后者等着重编译时自然更新。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

#: 各阶段 manifest 的固定文件名（放在对应产出目录下）
MANIFEST_L1 = "manifest_l1.json"
MANIFEST_L2 = "manifest_l2.json"

VERSION = 1

#: align 报告里 id 列表的截断上限 —— 全量清单看 align_report，接口别吐几千条
_LIST_CAP = 200


def manifest_path(out_dir: Path | str, stage: str) -> Path:
    """某产出目录下某阶段的 manifest 路径。"""
    return Path(out_dir) / ("manifest_%s.json" % stage)


def _fingerprint(content: str | None) -> str:
    """内容指纹：现算 md5，**不用**库里的 content_hash 字段。

    content_hash 只在蒸馏写入时生成；此后任何绕过蒸馏链路的直接改库
    （实测：overlong 截断修复 UPDATE 了 688 条 content，hash 未重算）都会让
    它与真实内容脱钩 —— 拿它做变更检测会漏报。自算指纹才反映"内容现在是什么"。
    """
    return hashlib.md5((content or "").encode("utf-8")).hexdigest()


def snapshot_inputs(conn: sqlite3.Connection) -> tuple[dict[int, str], dict[str, list[int]]]:
    """一次查询拿到 编译输入快照 + 会话归属映射（只读口径，与两个脚本一致）。

    返回：
      inputs   : {mid: 内容指纹}       —— 全库 new/similar 的内容指纹
      sessions : {"source/cid": [mid, ...]}  —— 按会话分组（与 LLM 看到的顺序一致）
    """
    inputs: dict[int, str] = {}
    sessions: dict[str, list[int]] = {}
    for mid, content, src, cid in conn.execute(
            "SELECT id, content, source, conversation_id"
            " FROM distilled_memories WHERE status IN ('new','similar')"
            " ORDER BY source, conversation_id, created_at, id"):
        inputs[mid] = _fingerprint(content)
        sessions.setdefault("%s/%s" % (src, cid), []).append(mid)
    return inputs, sessions


def build_manifest(stage: str, db_path: Path | str,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """对某索引库做输入快照，生成 manifest 结构（不落盘）。"""
    p = Path(db_path)
    conn = sqlite3.connect("file:%s?mode=ro" % p.as_posix(), uri=True)
    try:
        inputs, sessions = snapshot_inputs(conn)
    finally:
        conn.close()
    m: dict[str, Any] = {
        "version": VERSION,
        "stage": stage,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "src_db": str(p),
        "n_inputs": len(inputs),
        # json 的 key 必须是字符串，id 转成 str 存
        "inputs": {str(k): v for k, v in inputs.items()},
        "sessions": sessions,
    }
    if extra:
        m.update(extra)
    return m


def write_manifest(path: Path | str, manifest: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def load_manifest(path: Path | str) -> dict[str, Any] | None:
    """读 manifest；不存在或损坏返回 None（对齐审计要能区分'没有'与'坏'）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# diff：manifest（编译时） ↔ 索引库（现在）
# ---------------------------------------------------------------------------

def diff_manifest(manifest: dict[str, Any], db_path: Path | str) -> dict[str, Any]:
    """对比 manifest 与当前库，产出结构化变更集 + 脏会话映射。

    返回（id 列表已截断到 `_LIST_CAP`，总数看 *total 字段）：
      added / changed / removed           : 变更记忆 id
      removed_missing / removed_reclassified : removed 的细分
      added_total / changed_total / removed_total
      dirty_sessions : {"src/cid": {"added":n, "changed":n, "removed":n}}
      current_inputs : 当前输入总数
    """
    cur_inputs, cur_sessions = _snapshot_of(db_path)
    # json 键是字符串 —— 统一回 int 再比较（坏键跳过），否则全部误判成 added/removed
    old: dict[int, str] = {}
    for k, v in (manifest.get("inputs") or {}).items():
        try:
            old[int(k)] = v
        except (TypeError, ValueError):
            continue
    old_sessions: dict[str, list] = manifest.get("sessions") or {}

    added = sorted(int(k) for k in cur_inputs if k not in old)
    removed = sorted(int(k) for k in old if k not in cur_inputs)
    changed = sorted(int(k) for k in old
                     if k in cur_inputs and cur_inputs[k] != old[k])

    # removed 细分：库中彻底消失 vs 只是状态不再是输入
    missing: list[int] = []
    reclassified: list[int] = []
    if removed:
        conn = sqlite3.connect("file:%s?mode=ro" % Path(db_path).as_posix(), uri=True)
        try:
            q = ",".join("?" * len(removed))
            rows = dict(conn.execute(
                "SELECT id, status FROM distilled_memories WHERE id IN (%s)" % q,
                removed).fetchall())
        finally:
            conn.close()
        for mid in removed:
            (reclassified if mid in rows else missing).append(mid)

    # 脏会话：变更记忆 → 会话归属（先查 manifest 里的旧归属，查不到用当前库的）
    mid_to_sess: dict[int, str] = {}
    for sess, mids in old_sessions.items():
        for mid in mids:
            mid_to_sess[mid] = sess
    for sess, mids in cur_sessions.items():
        for mid in mids:
            mid_to_sess.setdefault(mid, sess)

    dirty: dict[str, dict[str, int]] = {}
    for kind, ids in (("added", added), ("changed", changed), ("removed", removed)):
        for mid in ids:
            d = dirty.setdefault(mid_to_sess.get(mid, "?"),
                                 {"added": 0, "changed": 0, "removed": 0})
            d[kind] += 1

    def _cap(lst: list[int]) -> list[int]:
        return lst[:_LIST_CAP]

    return {
        "added": _cap(added), "changed": _cap(changed), "removed": _cap(removed),
        "removed_missing": _cap(missing), "removed_reclassified": _cap(reclassified),
        "added_total": len(added), "changed_total": len(changed),
        "removed_total": len(removed),
        "dirty_sessions": dict(sorted(dirty.items())[:_LIST_CAP]),
        "dirty_session_total": len(dirty),
        "current_inputs": len(cur_inputs),
    }


def _snapshot_of(db_path: Path | str) -> tuple[dict[int, str], dict[str, list[int]]]:
    conn = sqlite3.connect("file:%s?mode=ro" % Path(db_path).as_posix(), uri=True)
    try:
        return snapshot_inputs(conn)
    finally:
        conn.close()
