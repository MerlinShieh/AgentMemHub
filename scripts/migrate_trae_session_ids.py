"""一次性迁移：给 trae 的存量会话 id 补类别前缀。

## 为什么要迁移

`agentmemhub/adapters/trae.py` 的三类数据（代码快照 / SOLO 任务 / 项目记忆）
共用 `source="trae"`，而库的主键是 `(source, id)` —— 三者目录名撞车时后写的
会整会话覆盖前面的。所以新版本给 id 加了 `snap:` / `solo:` / `mem:` 前缀。

但**存量行没有跟着迁移**。trae 没有轻量会话清单，`ingest trae` 每次都是整源
重扫，旧 id 既不会被删除（`store.upsert_sessions` 明确"不删除源端已消失的
会话"，只有 `--full` 的 `replace_source` 才清）也不会改名。于是下一次 ingest
之后同一份数据会以「旧 id + 新 id」两条并存：

- events 翻倍、L1/L2 页面双份、蒸馏候选双份；
- 旧 id 从此冻结不再更新，新 id 继续演化 —— 两边内容越走越远。

## 判据

`conversations.meta_json.kind` 是三类数据的权威标记（adapter 写入时就有）：
`snapshot` → `snap:`、`solo_task` → `solo:`、`project_memory` → `mem:`。
不靠 id 形态猜。

## 用法

    python scripts/migrate_trae_session_ids.py                  # 只打印计划（默认）
    python scripts/migrate_trae_session_ids.py --apply          # 真正执行
    python scripts/migrate_trae_session_ids.py --apply --db D:/path/agentmemhub.db

索引库（units / conv_scores）默认取同目录下的 `session_rag.db`，可用
`--index-db` 指定。脚本幂等：已带前缀的会话会被跳过。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SOURCE = "trae"
PREFIX = {"snapshot": "snap:", "solo_task": "solo:", "project_memory": "mem:"}


def _plan(cx: sqlite3.Connection) -> list[tuple[str, str, str]]:
    plan: list[tuple[str, str, str]] = []
    existing = {r[0] for r in cx.execute(
        "SELECT id FROM conversations WHERE source=?", (SOURCE,))}
    for cid, meta_json in cx.execute(
            "SELECT id, meta_json FROM conversations WHERE source=?", (SOURCE,)):
        if ":" in cid:                          # 已迁移（Windows 目录名不含冒号）
            continue
        try:
            kind = (json.loads(meta_json or "{}") or {}).get("kind")
        except (TypeError, ValueError):
            kind = None
        pre = PREFIX.get(kind or "")
        if not pre:
            print(f"  [跳过] {cid!r} 的 kind={kind!r} 不在已知三类内，需人工判断")
            continue
        new = pre + cid
        if new in existing:
            print(f"  [跳过] {cid!r} → {new!r} 已存在（可能部分迁移过）")
            continue
        plan.append((cid, new, kind or ""))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="给 trae 存量会话 id 补类别前缀")
    ap.add_argument("--db", default="database/agentmemhub.db",
                    help="采集库路径（conversations / events）")
    ap.add_argument("--index-db", default="",
                    help="索引库路径，缺省取 --db 同目录下的 session_rag.db")
    ap.add_argument("--apply", action="store_true", help="真正执行（缺省只打印计划）")
    a = ap.parse_args()

    db = Path(a.db)
    if not db.exists():
        print(f"采集库不存在：{db}")
        return 2
    idx = Path(a.index_db) if a.index_db else db.with_name("session_rag.db")

    cx = sqlite3.connect(db)
    cx.execute("PRAGMA busy_timeout=15000")
    try:
        plan = _plan(cx)
        if not plan:
            print("没有需要迁移的 trae 会话（已全部带前缀或库为空）")
            return 0
        print(f"待迁移 {len(plan)} 个会话：")
        for old, new, kind in plan:
            print(f"  {kind:14s} {old!r} → {new!r}")
        if not a.apply:
            print("\n（以上为计划，未写入。加 --apply 执行）")
            return 0

        with cx:                                # conversations / events 同一库同一事务
            for old, new, _kind in plan:
                cx.execute("UPDATE conversations SET id=? WHERE source=? AND id=?",
                           (new, SOURCE, old))
                n_e = cx.execute(
                    "UPDATE events SET conversation_id=? WHERE source=? AND conversation_id=?",
                    (new, SOURCE, old)).rowcount
                print(f"  会话 {old!r} → {new!r}：events {n_e} 行")
    finally:
        cx.close()

    if idx.exists():
        ix = sqlite3.connect(idx)
        ix.execute("PRAGMA busy_timeout=15000")
        try:
            with ix:
                for old, new, _kind in plan:
                    n_u = ix.execute(
                        "UPDATE units SET conversation_id=?"
                        " WHERE source=? AND conversation_id=?", (new, SOURCE, old)).rowcount
                    n_c = ix.execute(
                        "UPDATE conv_scores SET conversation_id=?"
                        " WHERE source=? AND conversation_id=?", (new, SOURCE, old)).rowcount
                    if n_u or n_c:
                        print(f"  索引 {old!r} → {new!r}：units {n_u} 行 / conv_scores {n_c} 行")
        finally:
            ix.close()
    else:
        print(f"（索引库不存在，跳过 units/conv_scores：{idx}）")

    print("\n迁移完成。建议接着跑：python scripts/health_check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
