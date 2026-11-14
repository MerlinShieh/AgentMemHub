#!/usr/bin/env python
"""把历史 MCP 记忆的虚拟会话桶迁移到「项目」。

## 背景

`memory_save` 的直写记忆此前落在 `direct-YYYY-MM-DD` **按日分桶**（虚拟会话），
只服务 wiki L1 的脏单位切分；它既不是真实会话，也不是项目。2026-11-14 起改为
按**项目**分桶（工作目录编码）。本脚本把历史 252 条迁移过去。

项目归属来自 `scripts/project_grouping_probe.py` 的 LLM 分组结果
（`exports/project_grouping_report/assignments.json`），**不是规则推导**——
改造前的记忆既无 session 也无 cwd，没有别的依据。

## 安全

- `--apply` 会**先建快照**（索引库 + L1/L2 产物），失败可用 `--rollback <sid>` 还原；
- `--plan` 只打印将要发生什么，不写库；
- 只动 `distilled_memories.conversation_id` 与 `units.conversation_id`
  （后者是召回面的投影，不改会出现"面板改名了、检索还在旧桶"）；
- 不删任何记忆。`second-brain-skill` 那 4 条按用户决定保留。

## 用法

    uv run python scripts/project_bucket_migrate.py --plan
    uv run python scripts/project_bucket_migrate.py --apply
    uv run python scripts/project_bucket_migrate.py --rollback <快照id>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ASSIGN_JSON = Path("exports/project_grouping_report/assignments.json")
PROJECTS_JSON = Path("exports/project_grouping_report/projects.json")


def load_plan() -> dict[int, tuple[str, str]]:
    """返回 {memory_id: (项目名, 项目编码)}。"""
    if not ASSIGN_JSON.exists() or not PROJECTS_JSON.exists():
        raise SystemExit(
            f"缺少分组结果：{ASSIGN_JSON} / {PROJECTS_JSON}\n"
            "先跑 scripts/project_grouping_probe.py")
    projects = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
    code = {p["name"]: (p.get("key") or "").strip() for p in projects}
    assigns = json.loads(ASSIGN_JSON.read_text(encoding="utf-8"))
    plan: dict[int, tuple[str, str]] = {}
    missing = []
    for a in assigns:
        name = a.get("project") or ""
        key = code.get(name, "")
        if not key:
            missing.append((a.get("id"), name))
            continue
        plan[int(a["id"])] = (name, key)
    if missing:
        print(f"⚠️ {len(missing)} 条没有项目编码，将跳过：{missing[:5]}…")
    return plan


def open_index_rw() -> sqlite3.Connection:
    from agentmemhub.rag.config import load_settings
    return sqlite3.connect(str(load_settings().index_db))


def survey(conn: sqlite3.Connection, plan: dict[int, tuple[str, str]]) -> dict:
    """统计：要改多少条、当前分布、会撞哪些约束。"""
    ids = list(plan)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT id, conversation_id, source FROM distilled_memories"
        f" WHERE id IN ({marks})", ids).fetchall()
    current = Counter(r[1] for r in rows)
    by_proj = Counter(plan[r[0]][0] for r in rows if r[0] in plan)
    # 投影行数（召回面）：units.legacy_id = 'mcp_' || tid，而蒸馏表
    # slice_key = 'mcp:' || tid（前缀一个是下划线、一个是冒号！）。实测：
    # 用 `legacy_id = 'mcp_' || substr(slice_key, 5)` 匹配 254 条；
    # 漏掉 'mcp_' 前缀则匹配 0 条（踩过）。
    proj_rows = conn.execute(
        "SELECT COUNT(*) FROM units u JOIN distilled_memories d"
        "  ON u.legacy_id = 'mcp_' || substr(d.slice_key, 5)"
        " WHERE u.source='memory' AND d.id IN (%s)" % ",".join("?" * len(ids)),
        ids).fetchone()[0]
    return {"mem_rows": len(rows), "current": current,
            "by_project": by_proj, "unit_rows": proj_rows}


def apply_plan(conn: sqlite3.Connection, plan: dict[int, tuple[str, str]]) -> dict:
    """在一个事务里改两张表。返回统计。"""
    mem_changed = unit_changed = 0
    with conn:                                  # 自动 commit / 异常回滚
        for mid, (_name, key) in plan.items():
            cur = conn.execute(
                "UPDATE distilled_memories SET conversation_id=?"
                " WHERE id=? AND conversation_id<>?", (key, mid, key))
            mem_changed += cur.rowcount
            # 召回面投影：units.legacy_id 带 `mcp_` 前缀，蒸馏表 slice_key 带
            # `mcp:` 前缀 —— 两者只差第 4 个字符，但**不能省**（漏了前缀会匹配
            # 0 行：面板改名了、检索却还在旧桶）。口径与 api_memories 的去重一致。
            cur = conn.execute(
                "UPDATE units SET conversation_id=?"
                " WHERE source='memory' AND legacy_id=("
                "   SELECT 'mcp_' || substr(slice_key, 5)"
                "   FROM distilled_memories WHERE id=?"
                " ) AND conversation_id<>?", (key, mid, key))
            unit_changed += cur.rowcount
    return {"mem_changed": mem_changed, "unit_changed": unit_changed}


def main() -> int:
    ap = argparse.ArgumentParser(description="历史 MCP 记忆 → 项目桶迁移")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="只预览，不写库")
    g.add_argument("--apply", action="store_true", help="执行（会先建快照）")
    g.add_argument("--rollback", metavar="SNAPSHOT_ID", help="按快照回滚")
    ap.add_argument("--yes", action="store_true", help="跳过 --apply 的确认")
    args = ap.parse_args()

    if args.rollback:
        from agentmemhub import snapshot
        print(f"回滚快照 {args.rollback} …")
        snapshot.restore(args.rollback, force=True)
        print("回滚完成")
        return 0

    plan = load_plan()
    if not plan:
        print("无可迁移条目")
        return 0
    conn = open_index_rw()
    try:
        st = survey(conn, plan)
        print(f"=== 待迁移 {len(plan)} 条 ===")
        print("  当前虚拟桶分布：")
        for k, v in st["current"].most_common():
            print(f"    {k:<22} {v:>4}")
        print("  迁移后项目分布：")
        for k, v in st["by_project"].most_common():
            print(f"    {k:<22} {v:>4}")
        print(f"  召回面投影行（units）：{st['unit_rows']}")

        if args.plan:
            print("\n（--plan 模式，未写入任何数据）")
            return 0
        if not args.yes:
            print("\n⚠️ 即将写库。确认请加 --yes 重跑。")
            return 1

        from agentmemhub import snapshot
        snap = snapshot.create(reason="项目分桶迁移（direct-* → 项目编码）")
        sid = snap["id"] if isinstance(snap, dict) else str(snap)
        print(f"\n已建快照：{sid}（{snap.get('size_mb')} MB）" if isinstance(snap, dict)
              else f"\n已建快照：{sid}")
        r = apply_plan(conn, plan)
        print(f"完成：蒸馏表 {r['mem_changed']} 行、召回投影 {r['unit_changed']} 行")
        print(f"回滚命令：uv run python scripts/project_bucket_migrate.py --rollback {sid}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
