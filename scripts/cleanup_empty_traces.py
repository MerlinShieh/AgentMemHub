# -*- coding: utf-8 -*-
"""清理 MemOS 引擎库中的空 trace（导入链路缺陷产生的历史脏数据）。

背景：memos.py 切轮逻辑曾把会话首条 user 消息之前的 meta 事件切成一条
userText/agentText/summary 全空的"幽灵轮次"，且 workbuddy 纯 shell 会话
整会话被压成无文本 trace。修复推送逻辑后，用本脚本一次性删除已入库的脏数据，
随后重推即可生成带标题的干净版本（trace id 由源锚点派生，天然幂等）。

安全约束：
- 引擎必须已停止（端口 18800 空闲），否则直接拒跑——避免与 daemon 抢写 WAL；
- 默认 dry-run 只报告，加 --yes 才真正删除；
- 只删 user_text 与 agent_text 同时为空的 trace，并级联清理：
  embedding_retry_queue 目标行、episodes.trace_ids_json 引用、
  失去全部 trace 的孤儿 episode / session。

用法：
    python scripts/cleanup_empty_traces.py            # 预览
    python scripts/cleanup_empty_traces.py --yes      # 执行
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "memOS" / "home" / "data" / "memos.db"
ENGINE_PORT = 18800


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    apply = "--yes" in sys.argv
    if port_busy(ENGINE_PORT):
        print(f"引擎仍在监听 :{ENGINE_PORT}，请先停止引擎再运行本脚本。")
        return 1
    if not DB_PATH.is_file():
        print(f"数据库不存在: {DB_PATH}")
        return 1

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT id, episode_id, session_id FROM traces "
            "WHERE COALESCE(user_text,'')='' AND COALESCE(agent_text,'')=''"
        ).fetchall()
        if not rows:
            print("没有空 trace，无需清理。")
            return 0
        ids = [r["id"] for r in rows]
        eps = sorted({r["episode_id"] for r in rows if r["episode_id"]})
        sids = sorted({r["session_id"] for r in rows if r["session_id"]})
        ph = ",".join("?" * len(ids))
        print(f"待删除空 trace: {len(ids)}；涉及 episode: {len(eps)}，session: {len(sids)}")

        # 级联引用统计（仅提示；除 retry_queue 外均为软引用）
        rq = db.execute(
            "DELETE FROM embedding_retry_queue WHERE target_kind='trace' "
            f"AND target_id IN ({ph})", ids).rowcount if apply else db.execute(
            "SELECT COUNT(*) c FROM embedding_retry_queue WHERE target_kind='trace' "
            f"AND target_id IN ({ph})", ids).fetchone()["c"]
        print(f"embedding_retry_queue 关联行: {rq}")

        # 删除后将成为孤儿的 episode/session（其剩余 trace 数为 0）
        orphan_eps = [e for e in eps if db.execute(
            "SELECT COUNT(*) c FROM traces WHERE episode_id=? "
            f"AND id NOT IN ({ph})", [e] + ids).fetchone()["c"] == 0]
        orphan_sids = [s for s in sids if db.execute(
            "SELECT COUNT(*) c FROM traces WHERE session_id=? "
            f"AND id NOT IN ({ph})", [s] + ids).fetchone()["c"] == 0]
        print(f"将一并删除孤儿 episode: {len(orphan_eps)}，孤儿 session: {len(orphan_sids)}")

        if not apply:
            print("dry-run 结束（加 --yes 执行）。")
            db.rollback()
            return 0

        deleted = db.execute(f"DELETE FROM traces WHERE id IN ({ph})", ids).rowcount
        # 从存活 episode 的 trace_ids_json 中摘除已删 id
        eph = ",".join("?" * len(eps))
        id_set = set(ids)
        for ep in db.execute(f"SELECT id, trace_ids_json FROM episodes WHERE id IN ({eph})", eps):
            try:
                arr = json.loads(ep["trace_ids_json"] or "[]")
            except Exception:
                continue
            kept = [t for t in arr if t not in id_set]
            if len(kept) != len(arr):
                db.execute("UPDATE episodes SET trace_ids_json=? WHERE id=?",
                           (json.dumps(kept), ep["id"]))
        if orphan_eps:
            oeph = ",".join("?" * len(orphan_eps))
            db.execute(f"DELETE FROM episodes WHERE id IN ({oeph})", orphan_eps)
        if orphan_sids:
            osph = ",".join("?" * len(orphan_sids))
            db.execute(f"DELETE FROM sessions WHERE id IN ({osph})", orphan_sids)
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        left = db.execute(
            "SELECT COUNT(*) c FROM traces WHERE COALESCE(user_text,'')='' "
            "AND COALESCE(agent_text,'')=''").fetchone()["c"]
        total = db.execute("SELECT COUNT(*) c FROM traces").fetchone()["c"]
        print(f"完成：删除 trace {deleted} 条、孤儿 episode {len(orphan_eps)}、"
              f"孤儿 session {len(orphan_sids)}；剩余空 trace {left}，全库 trace {total}。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
