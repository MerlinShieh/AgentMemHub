"""R4 存量迁移：MemOS memos.db → session_rag.db（units/unit_values/unit_feedback/legacy_id）。

只读 memos.db，写索引库。幂等：units 按 legacy_id 去重；feedback 审计按
(unit_id, polarity, magnitude, created_at) 四元组去重，可安全重跑。

映射规则（与 agentmemhub/memos.py 锚点算法逐字一致）：
- mcp_* 原子记忆 → 新 unit（source='memory'，legacy_id=原 id，memos 现值灌 unit_values）
- trac_* 会话轨迹 → 按锚点反查既有 user unit（_id([source,conv,src_id|seq:N],'trac')）
  仅补 legacy_id 别名 + 灌分，不复制文本
- feedback 全量 → value=clamp(Σsigned·mag/Σmag,±1)、priority=max(预置,|v|)
  （MemOS 同式），原始明细落 unit_feedback 审计
- 迁移尾 sync_conv_scores 全量重建会话级 r_task
- scored_traces.json 无需改写：其 id 即迁移后 refId（legacy_id 优先解析）

用法：
  uv run python scripts/migrate_memos_to_rag.py            # dry-run 报告
  uv run python scripts/migrate_memos_to_rag.py --apply    # 真迁移
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmemhub import memos_daemon, rag_bridge                 # noqa: E402
from agentmemhub.memos import _id                                # noqa: E402
from agentmemhub.rag.ingest import (                             # noqa: E402
    ensure_bridge_schema, ensure_vec_table, open_index)
from agentmemhub.rag.memstore import (                           # noqa: E402
    POLARITY_SIGN, ensure_memstore_schema)
from agentmemhub.rag.runtime import get_active_embedder          # noqa: E402


def _memos_conn() -> sqlite3.Connection:
    home = memos_daemon.engine_home()
    p = (home / "data" / "memos.db") if home else None
    if not p or not p.exists():
        raise SystemExit(f"memos.db 不存在：{p}（确认 memos.home / memOS 目录）")
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_anchor_index(conn: sqlite3.Connection) -> dict[str, int]:
    """trac_* 反查表：_id([source,conv,anchor],'trac') → user unit id。"""
    out: dict[str, int] = {}
    for uid, source, cid, src_id, seq in conn.execute(
            "SELECT id, source, conversation_id, src_id, seq FROM units"
            " WHERE role='user' AND source<>'memory'"):
        for a in ((str(src_id) if src_id else None), f"seq:{seq}"):
            if a:
                out[_id([source, cid, a], "trac")] = uid
    return out


def _feedback_aggregates(mc: sqlite3.Connection) -> dict[str, tuple[float, float, int]]:
    """trace_id → (signed, total_mag, n_rows)。magnitude 钳 0..1（MemOS 同规则）。"""
    acc: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for r in mc.execute("SELECT trace_id, polarity, magnitude FROM feedback"):
        mag = min(max(float(r["magnitude"] or 0.0), 0.0), 1.0)
        signed = POLARITY_SIGN.get(r["polarity"], 0.0) * mag
        a = acc[r["trace_id"]]
        a[0] += signed
        a[1] += mag
        a[2] += 1
    return {k: (v[0], v[1], v[2]) for k, v in acc.items()}


def _seed_values(conn: sqlite3.Connection, unit_id: int, t, aggs, now):
    preset_v = float(t["value"] or 0.0)
    preset_p = float(t["priority"] or 0.0)
    signed, total, _n = aggs.get(t["id"], (0.0, 0.0, 0))
    value = max(min(signed / total, 1.0), -1.0) if total else preset_v
    priority = max(preset_p, abs(value))
    if value or priority:
        conn.execute(
            "INSERT OR REPLACE INTO unit_values(unit_id, value, r_human, priority,"
            " updated_at) VALUES(?,?,?,?,?)",
            (unit_id, value, value if value else None, priority, now))
        return True
    return False


def migrate(apply: bool) -> dict:
    mc = _memos_conn()
    traces = [dict(r) for r in mc.execute(
        "SELECT id, user_text, agent_text, summary, ts, value, priority FROM traces")]
    feedback_rows = [dict(r) for r in mc.execute(
        "SELECT trace_id, channel, polarity, magnitude, ts FROM feedback")]
    mc.close()
    aggs = _feedback_aggregates_from_rows(feedback_rows)

    st = rag_bridge.settings()
    conn = open_index(st.index_db)
    ensure_bridge_schema(conn)
    ensure_memstore_schema(conn)
    ensure_vec_table(conn, st.active_spec)

    anchor_map = build_anchor_index(conn)
    existing_legacy = {r[0] for r in conn.execute(
        "SELECT legacy_id FROM units WHERE legacy_id IS NOT NULL")}

    mcp_new, trac_match, trac_unmatched = [], {}, []
    for t in traces:
        tid = t["id"]
        if tid in existing_legacy:
            continue
        if tid.startswith("mcp_"):
            mcp_new.append(t)
        elif tid in anchor_map:
            trac_match[anchor_map[tid]] = t
        else:
            trac_unmatched.append(t)
    # 未匹配 trac_ 三分法：正文全新（零丢失落单元）/ 正文已入库（跳过防双份）/ 空幽灵（计数）
    unit_texts = [r[0] or "" for r in conn.execute("SELECT text FROM units")]
    known_prefix = {u[:400] for u in unit_texts}
    unit_blob = "\n@@\n".join(unit_texts)

    def _already_in_db(text: str) -> bool:
        # 前缀精确 或 正文头部 60 字符被既有单元包含（memos 的 user_text 常带
        # 注入包裹壳、units 是清洗后的裸文本，需两种口径都判）
        return text[:400] in known_prefix or text[:60] in unit_blob

    orphan_trac, dupe_skipped, ghost_trac = [], [], []
    for t in trac_unmatched:
        text = (t["user_text"] or t["agent_text"] or t["summary"] or "").strip()
        if not text:
            ghost_trac.append(t)
        elif _already_in_db(text):
            dupe_skipped.append(t)
        else:
            orphan_trac.append(t)
    new_units = mcp_new + orphan_trac

    now = int(time.time())
    stats = {"memos_traces": len(traces), "feedback_rows": len(feedback_rows),
             "mcp_new": len(mcp_new), "trac_to_link": len(trac_match),
             "trac_orphan_content": len(orphan_trac),
             "trac_dupe_skipped": len(dupe_skipped),
             "trac_ghost_dropped": len(ghost_trac),
             "mcp_migrated": 0, "trac_linked": 0, "valued_total": 0,
             "applied": apply}

    if not apply:
        conn.close()
        stats["orphan_sample"] = [
            {"id": t["id"], "user": (t["user_text"] or t["agent_text"] or "")[:40]}
            for t in orphan_trac[:5]]
        return stats

    # mcp_* → memory units（批量嵌入）
    emb = get_active_embedder(st)
    pend = [(t, (t["user_text"] or t["agent_text"] or t["summary"] or "").strip())
            for t in new_units]
    pend = [(t, x) for t, x in pend if x]
    for i in range(0, len(pend), 64):
        chunk = pend[i:i + 64]
        vecs = emb.encode_passages([x for _t, x in chunk])
        with conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM units WHERE source='memory'"
            ).fetchone()[0]
            for (t, text), v in zip(chunk, vecs):
                seq += 1
                srcid = ("mig_" + t["id"] if t["id"].startswith("mcp_")
                         else "migtrac_" + t["id"])
                cur = conn.execute(
                    "INSERT INTO units(source, conversation_id, seq, role,"
                    " turn_key, src_id, time, title, text, chars, legacy_id)"
                    " VALUES('memory','mcp',?,'user','mcp',?,?,?,?,?,?)",
                    (seq, srcid, int((t["ts"] or 0) / 1000),
                     "记忆", text, len(text), t["id"]))
                conn.execute(
                    f"INSERT OR REPLACE INTO {st.active_spec.vec_table}"
                    "(rowid, embedding) VALUES(?,?)",
                    (cur.lastrowid, v.astype(__import__("numpy").float32).tobytes()))
                _seed_values(conn, cur.lastrowid, t, aggs, now)
                stats["mcp_migrated"] += 1

    # trac_* → 补别名 + 灌分
    with conn:
        for uid, t in trac_match.items():
            conn.execute(
                "UPDATE units SET legacy_id=? WHERE id=? AND legacy_id IS NULL",
                (t["id"], uid))
            _seed_values(conn, uid, t, aggs, now)
            stats["trac_linked"] += 1

    # feedback 审计明细（四元组去重，幂等）
    legacy2unit = {r[0]: r[1] for r in conn.execute(
        "SELECT legacy_id, id FROM units WHERE legacy_id IS NOT NULL")}
    have = {(r[0], r[1], r[2], r[3]) for r in conn.execute(
        "SELECT unit_id, polarity, magnitude, created_at FROM unit_feedback")}
    with conn:
        for f in feedback_rows:
            uid = legacy2unit.get(f["trace_id"])
            if uid is None:
                continue
            key = (uid, f["polarity"], round(float(f["magnitude"] or 0.0), 6),
                   int((f["ts"] or 0) / 1000))
            if key in have:
                continue
            conn.execute(
                "INSERT INTO unit_feedback(unit_id, channel, polarity, magnitude,"
                " created_at) VALUES(?,?,?,?,?)",
                (uid, f["channel"] or "explicit", f["polarity"],
                 float(f["magnitude"] or 0.0), key[3]))
            have.add(key)
    stats["valued_total"] = conn.execute(
        "SELECT COUNT(*) FROM unit_values").fetchone()[0]
    conn.close()
    stats["conv_rollup"] = rag_bridge.sync_conv_scores()
    return stats


def _feedback_aggregates_from_rows(rows) -> dict[str, tuple[float, float, int]]:
    acc: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for f in rows:
        mag = min(max(float(f["magnitude"] or 0.0), 0.0), 1.0)
        a = acc[f["trace_id"]]
        a[0] += POLARITY_SIGN.get(f["polarity"], 0.0) * mag
        a[1] += mag
        a[2] += 1
    return {k: (v[0], v[1], v[2]) for k, v in acc.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="memos.db → session_rag.db 存量迁移")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    args = ap.parse_args(argv)
    rep = migrate(args.apply)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if rep.get("trac_ghost_dropped"):
        print("（已丢弃空幽灵轮次 trac_ghost_dropped 条：memos 中无正文，无需保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
