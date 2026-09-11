"""导出蒸馏记忆为**可移植备份**（脱离本项目也能二次使用）。

两种形态，各有用处：
- `memories.jsonl`：一行一条记忆，纯文本、无依赖，任何语言/工具都能直接消费；
  含完整溯源字段（来源会话、标题、轮次、类型、置信度、模型、时间）；
- `manifest.json`：导出元数据 + 各文件 sha256（校验完整性）+ 分布统计；
- `--sqlite` 时额外产出索引库完整副本（含向量表，可在别处原样还原检索能力）。

用法：
  export AGENTMEM_HUB_DATA_DIR=<沙箱或数据目录>
  uv run python scripts/export_memories.py                       # 默认 exports/memories-<时间戳>/
  uv run python scripts/export_memories.py --out D:/backup/mem   # 指定目录
  uv run python scripts/export_memories.py --all                 # 含被归档的段级条目
  uv run python scripts/export_memories.py --sqlite              # 追加完整库副本

设计约束：导出**只读**，绝不修改源数据。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FORMAT_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return ""


def _load_titles(source_db: Path) -> dict[tuple[str, str], str]:
    """采集库的会话标题（导入侧可选；缺失时留空，不影响导出）。"""
    if not source_db.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
        try:
            return {(r[0], r[1]): (r[2] or "")
                    for r in c.execute("SELECT source, id, title FROM conversations")}
        finally:
            c.close()
    except Exception:
        return {}


def export(out_dir: Path, *, include_all: bool = False,
           with_sqlite: bool = False) -> int:
    from agentmemhub import rag_bridge
    s = rag_bridge.settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    titles = _load_titles(s.source_db)

    conn = sqlite3.connect(f"file:{s.index_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where = "" if include_all else "WHERE status IN ('new','similar')"
        rows = conn.execute(
            f"SELECT id, source, conversation_id, slice_key, turn_key, type,"
            f" topic, content, confidence, status, dedup_of, content_hash,"
            f" prompt_ver, model, merged_from_json, created_at"
            f" FROM distilled_memories {where} ORDER BY source, conversation_id, id"
        ).fetchall()
    finally:
        conn.close()

    jsonl = out_dir / "memories.jsonl"
    by_type: Counter = Counter()
    by_status: Counter = Counter()
    by_source: Counter = Counter()
    with jsonl.open("w", encoding="utf-8") as f:
        for r in rows:
            rec = {
                "id": r["id"],
                "type": r["type"],
                "topic": r["topic"] or "",
                "content": r["content"],
                "confidence": r["confidence"],
                "status": r["status"],
                # 溯源（脱离本项目时靠这些字段还原上下文）
                "source": r["source"],
                "conversation_id": r["conversation_id"],
                "conversation_title": titles.get((r["source"], r["conversation_id"]), ""),
                "slice_key": r["slice_key"] or "",
                "turn_key": r["turn_key"] or "",
                # 幂等/审计
                "content_hash": r["content_hash"],
                "dedup_of": r["dedup_of"],
                "prompt_ver": r["prompt_ver"],
                "distill_model": r["model"] or "",
                "merged_from": json.loads(r["merged_from_json"])
                if r["merged_from_json"] else None,
                "created_at": r["created_at"],
                "created_at_iso": _iso(r["created_at"]),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            by_type[r["type"]] += 1
            by_status[r["status"]] += 1
            by_source[r["source"]] += 1

    files = {"memories.jsonl": {"lines": len(rows), "sha256": _sha256(jsonl)}}
    if with_sqlite:
        dst = out_dir / "session_rag.full.db"
        src = sqlite3.connect(f"file:{s.index_db.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(str(dst))
        try:
            src.backup(d)                 # 一致性快照（含 units 与向量表）
        finally:
            d.close()
            src.close()
        files[dst.name] = {"bytes": dst.stat().st_size, "sha256": _sha256(dst)}

    manifest = {
        "format_version": FORMAT_VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": "AgentMemHub scripts/export_memories.py",
        "note": "蒸馏记忆的可移植备份：memories.jsonl 一行一条，无项目依赖。",
        "index_db": str(s.index_db),
        "source_db": str(s.source_db),
        "embedding_model": s.active_model,
        "counts": {
            "total": len(rows),
            "by_type": dict(by_type),
            "by_status": dict(by_status),
            "by_source": dict(by_source.most_common()),
        },
        "include_archived": include_all,
        "files": files,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"✓ 导出完成 → {out_dir}")
    print(f"  memories.jsonl：{len(rows)} 条（sha256 {files['memories.jsonl']['sha256'][:12]}…）")
    print(f"  manifest.json ：元数据 + 校验值")
    if with_sqlite:
        mb = files['session_rag.full.db']['bytes'] / 1048576
        print(f"  session_rag.full.db：{mb:.0f} MB（完整库副本，含向量）")
    print(f"\n  类型分布：{dict(by_type)}")
    print(f"  状态分布：{dict(by_status)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="导出蒸馏记忆备份（可移植 JSONL）")
    ap.add_argument("--out", default="", help="输出目录（默认 exports/memories-<时间戳>）")
    ap.add_argument("--all", action="store_true",
                    help="含被归档的段级条目（默认只导出 new/similar 终稿）")
    ap.add_argument("--sqlite", action="store_true", help="追加导出完整索引库副本")
    args = ap.parse_args()

    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "exports" / f"memories-{time.strftime('%Y%m%d-%H%M%S')}")
    return export(out, include_all=args.all, with_sqlite=args.sqlite)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
