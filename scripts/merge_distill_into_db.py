"""把沙箱蒸馏成果合并进目标索引库（生产应用）。

迁移内容（四表 + 向量）：
  distilled_memories（3928：终稿+归档，保留 merge 上下文）
  distill_hashes（切片/合并指纹 —— 必须迁，否则重跑会全量重蒸）
  units role='distilled'（投影，id **重映射** 避开目标既有 id）
  unit_values（来源初始分/手动加权，unit_id 随重映射）
  vec_<model>（向量行，rowid 随重映射）

安全设计：
  · 先 --dry-run 预览映射与计数；
  · 全部写入在**单个事务**内，任何失败整体回滚；
  · 幂等可重跑：所有插入先查存在（memories 按 UNIQUE 三元组、units 按 src_id、
    hashes 按主键、values/vec 按 id），存在即跳过；
  · 向量读取分批（2k 行/批），目标库 busy_timeout 抗面板并发写。

用法：
  uv run python scripts/merge_distill_into_db.py --dry-run      # 预览
  uv run python scripts/merge_distill_into_db.py                # 执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = PROJECT_ROOT / "database_distill_test" / "session_rag.db"
TARGET_DB = PROJECT_ROOT / "database" / "session_rag.db"
BATCH = 2000

sys.path.insert(0, str(PROJECT_ROOT))


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description="蒸馏成果合并进目标索引库")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    ap.add_argument("--source", default=str(SOURCE_DB))
    ap.add_argument("--target", default=str(TARGET_DB))
    args = ap.parse_args()
    src_path, tgt_path = Path(args.source), Path(args.target)
    for p in (src_path, tgt_path):
        if not p.is_file():
            print(f"✗ 库不存在：{p}")
            return 1

    from agentmemhub.rag.config import load_settings
    from agentmemhub.rag.ingest import ensure_vec_table
    from agentmemhub.rag.memstore import ensure_memstore_schema
    from agentmemhub.distill import ensure_distill_schema

    settings = load_settings()
    spec = settings.active_spec
    vec_table = spec.vec_table

    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    src.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(src)                 # vec0 虚表查询需要扩展（只读侧同样要加载）
    src.enable_load_extension(False)
    src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(str(tgt_path), timeout=30.0)
    tgt.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(tgt)                 # 目标写 vec0 虚表同样需要扩展
    tgt.enable_load_extension(False)
    tgt.execute("PRAGMA busy_timeout=30000")
    try:
        # ---- 目标 schema 就绪（幂等）----
        ensure_distill_schema(tgt)
        ensure_memstore_schema(tgt)
        ensure_vec_table(tgt, spec)

        next_id = tgt.execute("SELECT COALESCE(MAX(id),0)+1 FROM units").fetchone()[0]
        print(f"目标：{tgt_path}")
        print(f"  units 现有 max(id)={next_id - 1}，蒸馏投影将从 {next_id} 起重映射")

        # ---- 读取源数据 ----
        mems = src.execute(
            "SELECT id, source, conversation_id, slice_key, turn_key, type, topic,"
            " content, confidence, status, dedup_of, content_hash, prompt_ver,"
            " model, merged_from_json, created_at"
            " FROM distilled_memories ORDER BY id").fetchall()
        hashes = src.execute(
            "SELECT source, conversation_id, slice_key, content_hash, prompt_ver,"
            " model, created_at FROM distill_hashes").fetchall()
        units = src.execute(
            "SELECT id, source, conversation_id, seq, turn_key, src_id, time,"
            " title, text, chars FROM units WHERE role='distilled' ORDER BY id"
        ).fetchall()
        unit_ids = [r["id"] for r in units]
        values = {}
        for i in range(0, len(unit_ids), BATCH):
            marks = ",".join("?" * len(unit_ids[i:i + BATCH]))
            for r in src.execute(
                    f"SELECT unit_id, value, r_human, manual_value, priority, updated_at"
                    f" FROM unit_values WHERE unit_id IN ({marks})",
                    unit_ids[i:i + BATCH]):
                values[r["unit_id"]] = r

        # ---- id 重映射 ----
        idmap = {old: next_id + i for i, old in enumerate(unit_ids)}
        collisions = [old for old in unit_ids
                      if old <= next_id - 1 and old in
                      {r[0] for r in tgt.execute("SELECT id FROM units")}]

        print(f"\n源：{src_path}")
        print(f"  memories {len(mems)} | hashes {len(hashes)} | "
              f"蒸馏投影 {len(units)} | values {len(values)}")
        if collisions:
            print(f"  ⚠ {len(collisions)} 个 id 与目标冲突，已全部重映射")
        print(f"  映射示例：{unit_ids[0]}→{idmap[unit_ids[0]]} … "
              f"{unit_ids[-1]}→{idmap[unit_ids[-1]]}")

        # ---- 目标已存在内容的跳过统计 ----
        exist_mem = tgt.execute(
            "SELECT COUNT(*) FROM distilled_memories").fetchone()[0]
        exist_units = tgt.execute(
            "SELECT COUNT(*) FROM units WHERE role='distilled'").fetchone()[0]
        print(f"  目标已有：memories {exist_mem} / 投影 {exist_units}"
              f"（存在即跳过，脚本幂等）\n")

        if args.dry_run:
            print("（--dry-run：未写入）")
            return 0

        # ---- 单事务写入 ----
        inserted = {"memories": 0, "hashes": 0, "units": 0, "values": 0, "vecs": 0}
        with tgt:
            for r in mems:
                dup = tgt.execute(
                    "SELECT 1 FROM distilled_memories WHERE source=? AND"
                    " conversation_id=? AND content_hash=?",
                    (r["source"], r["conversation_id"], r["content_hash"])).fetchone()
                if dup:
                    continue
                tgt.execute(
                    "INSERT INTO distilled_memories"
                    "(id, source, conversation_id, slice_key, turn_key, type, topic,"
                    " content, confidence, status, dedup_of, content_hash,"
                    " prompt_ver, model, merged_from_json, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["source"], r["conversation_id"], r["slice_key"],
                     r["turn_key"], r["type"], r["topic"], r["content"],
                     r["confidence"], r["status"], r["dedup_of"], r["content_hash"],
                     r["prompt_ver"], r["model"], r["merged_from_json"],
                     r["created_at"]))
                inserted["memories"] += 1

            for r in hashes:
                dup = tgt.execute(
                    "SELECT 1 FROM distill_hashes WHERE source=? AND"
                    " conversation_id=? AND slice_key=? AND content_hash=?"
                    " AND prompt_ver=?",
                    (r["source"], r["conversation_id"], r["slice_key"],
                     r["content_hash"], r["prompt_ver"])).fetchone()
                if dup:
                    continue
                tgt.execute(
                    "INSERT INTO distill_hashes"
                    "(source, conversation_id, slice_key, content_hash, prompt_ver,"
                    " model, created_at) VALUES(?,?,?,?,?,?,?)",
                    (r["source"], r["conversation_id"], r["slice_key"],
                     r["content_hash"], r["prompt_ver"], r["model"],
                     r["created_at"]))
                inserted["hashes"] += 1

            for r in units:
                new_id = idmap[r["id"]]
                if tgt.execute("SELECT 1 FROM units WHERE src_id=?",
                               (r["src_id"],)).fetchone():
                    continue
                tgt.execute(
                    "INSERT INTO units(id, source, conversation_id, seq, role,"
                    " turn_key, src_id, time, title, text, chars)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id, r["source"], r["conversation_id"], r["seq"],
                     "distilled", r["turn_key"], r["src_id"], r["time"],
                     r["title"], r["text"], r["chars"]))
                inserted["units"] += 1
                v = values.get(r["id"])
                if v is not None:
                    tgt.execute(
                        "INSERT OR IGNORE INTO unit_values"
                        "(unit_id, value, r_human, priority, manual_value,"
                        " updated_at) VALUES(?,?,?,?,?,?)",
                        (new_id, v["value"], v["r_human"], v["priority"],
                         v["manual_value"], v["updated_at"]))
                    inserted["values"] += 1

        # 向量分批写入（触发器外：vec0 无触发器，逐行 INSERT OR REPLACE）
        for i in range(0, len(unit_ids), BATCH):
            chunk = unit_ids[i:i + BATCH]
            marks = ",".join("?" * len(chunk))
            rows = src.execute(
                f"SELECT rowid, embedding FROM {vec_table} WHERE rowid IN ({marks})",
                chunk).fetchall()
            with tgt:
                for rid, emb in rows:
                    new_id = idmap[rid]
                    have = tgt.execute(
                        f"SELECT 1 FROM {vec_table} WHERE rowid=?", (new_id,)).fetchone()
                    if have:
                        continue
                    tgt.execute(
                        f"INSERT INTO {vec_table}(rowid, embedding) VALUES(?,?)",
                        (new_id, np.ascontiguousarray(
                            np.frombuffer(emb, dtype=np.float32),
                            dtype=np.float32).tobytes()))
                    inserted["vecs"] += 1

        n_units = tgt.execute(
            "SELECT COUNT(*) FROM units WHERE role='distilled'").fetchone()[0]
        n_mems = tgt.execute(
            "SELECT COUNT(*) FROM distilled_memories").fetchone()[0]
        print(f"\n✓ 迁移完成：{inserted}")
        print(f"  目标 units 蒸馏投影：{n_units}")
        print(f"  目标 memories：{n_mems}")
        return 0
    finally:
        src.close()
        tgt.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
