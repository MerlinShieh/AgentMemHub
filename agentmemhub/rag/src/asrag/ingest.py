"""摄取管道：源库(只读) → units(文本,模型无关) → vec_<model>(向量,按模型隔离)。

幂等契约：
- units 主键 UNIQUE(source, conversation_id, seq)，双跑零重复；
- 批间 keyset 分页按 (source, conversation_id, seq) 全序推进，
  固定 batch_size → 向量位级可复现（q8 漂移规则见 AGENTS.md 不变量3）。
溯源：每批日志记录水位、拉取/嵌入/跳过计数、耗时。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import numpy as np
import sqlite_vec

from .config import ModelSpec, Settings
from .embedder import Embedder, OnnxEmbedder
from .source import DEFAULT_ROLES, open_source_ro, prep_text, validate_source_schema

_SCHEMA_UNITS = """
CREATE TABLE IF NOT EXISTS units(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    turn_key TEXT,
    src_id TEXT,
    time INTEGER,
    title TEXT,
    text TEXT NOT NULL,
    chars INTEGER NOT NULL,
    UNIQUE(source, conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS ingest_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def open_index(path: Path | str) -> sqlite3.Connection:
    """打开/创建索引库并加载 sqlite-vec 扩展。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA journal_mode=WAL")
    # Python 3.12+ 默认 authorizer 拦截 load_extension，须显式放行（仅此处、加载后即关闭）
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_SCHEMA_UNITS)
    return conn


def ensure_vec_table(conn: sqlite3.Connection, spec: ModelSpec) -> None:
    """建（或校验）该模型的向量表。维度与注册表/既有表冲突必须报错。"""
    t = spec.vec_table
    meta_key = f"vecdim.{t}"
    row = conn.execute(
        "SELECT value FROM ingest_meta WHERE key=?", (meta_key,)
    ).fetchone()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone()
    if exists and row is None:
        raise RuntimeError(f"向量表 {t} 已存在但缺元数据，请用 --rebuild 重建")
    if row and int(row[0]) != spec.dim:
        raise RuntimeError(
            f"注册表 dim={spec.dim} 与既有索引 {t} dim={row[0]} 冲突——"
            f"同 id 换模型须先 --rebuild"
        )
    if not exists:
        conn.execute(
            f"CREATE VIRTUAL TABLE {t} USING vec0("
            f"embedding float[{spec.dim}] distance_metric=cosine)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO ingest_meta(key,value) VALUES(?,?)",
            (meta_key, str(spec.dim)),
        )
        conn.commit()


def gc_vec_orphans(conn: sqlite3.Connection, vec_table: str) -> int:
    """清理 units 已不存在的向量孤儿行（vec0 无 FK/触发器，删行后必须显式 GC）。

    vec0 仅稳定支持 rowid 等值删除，故先查孤儿再逐行删。返回删除数。
    """
    orphans = [
        r[0]
        for r in conn.execute(
            f"SELECT v.rowid FROM {vec_table} v"
            " WHERE v.rowid NOT IN (SELECT id FROM units)"
        )
    ]
    for rid in orphans:
        conn.execute(f"DELETE FROM {vec_table} WHERE rowid = ?", (rid,))
    if orphans:
        conn.commit()
    return len(orphans)


def drop_vec_table(conn: sqlite3.Connection, spec: ModelSpec) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {spec.vec_table}")
    conn.execute(
        "DELETE FROM ingest_meta WHERE key IN (?, ?)",
        (f"vecdim.{spec.vec_table}", f"vecbuilt.{spec.vec_table}"),
    )
    conn.commit()


def _existing_keys(conn: sqlite3.Connection) -> set[tuple]:
    return {
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT source, conversation_id, seq FROM units"
        )
    }


def _iter_candidates(
    src: sqlite3.Connection,
    roles: tuple[str, ...],
    after: tuple[str, str, int] | None,
    batch_size: int,
    limit: int | None,
) -> Iterator[list[sqlite3.Row]]:
    """源库侧 keyset 分页（全序），每片取 batch_size 行候选。"""
    fetched = 0
    cursor = after
    placeholders = ",".join("?" * len(roles))
    while limit is None or fetched < limit:
        want = batch_size
        if limit is not None:
            want = min(batch_size, limit - fetched)
        sql = f"""
            SELECT e.source, e.conversation_id, e.seq, e.role, e.turn_key,
                   e.src_id, e.time, c.title, e.content
            FROM events e
            LEFT JOIN conversations c
                   ON c.source = e.source AND c.id = e.conversation_id
            WHERE e.role IN ({placeholders})
              AND e.content IS NOT NULL AND TRIM(e.content) <> ''
        """
        args: list = list(roles)
        if cursor is not None:
            sql += " AND (e.source, e.conversation_id, e.seq) > (?, ?, ?)"
            args += list(cursor)
        sql += " ORDER BY e.source, e.conversation_id, e.seq LIMIT ?"
        args.append(want)
        rows = src.execute(sql, args).fetchall()
        if not rows:
            return
        cursor = (rows[-1]["source"], rows[-1]["conversation_id"], rows[-1]["seq"])
        fetched += len(rows)
        yield rows


def run_ingest(
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    source_db: Path | str | None = None,
    index_db: Path | str | None = None,
    roles: tuple[str, ...] | None = None,
    batch_size: int = 32,
    limit: int | None = None,
    rebuild: bool = False,
    log: logging.Logger | None = None,
) -> dict:
    """增量摄取（rebuild=True 时清空索引全量重建）。返回汇总统计。"""
    log = log or logging.getLogger("asrag.ingest")
    spec = settings.active_spec
    src_path = Path(source_db) if source_db else settings.source_db
    idx_path = Path(index_db) if index_db else settings.index_db
    roles = tuple(roles or DEFAULT_ROLES)
    embedder = embedder or OnnxEmbedder(spec, batch_size=batch_size, log=log)

    idx = open_index(idx_path)
    try:
        if rebuild:
            log.info("rebuild requested: dropping units + %s", spec.vec_table)
            drop_vec_table(idx, spec)
            idx.execute("DELETE FROM units")
            idx.execute("DELETE FROM ingest_meta WHERE key='watermark'")
            idx.commit()
        ensure_vec_table(idx, spec)

        src = open_source_ro(src_path)
        try:
            validate_source_schema(src)
            known = _existing_keys(idx)
            cursor = None
            summary = {
                "scanned": 0, "embedded": 0, "skipped_known": 0,
                "skipped_empty": 0, "batches": 0, "seconds": 0.0,
                "model": spec.id, "limit": limit,
            }
            t_all = time.perf_counter()
            for rows in _iter_candidates(src, roles, cursor, batch_size, limit):
                fresh: list[sqlite3.Row] = []
                for r in rows:
                    key = (r["source"], r["conversation_id"], r["seq"])
                    if key in known:
                        summary["skipped_known"] += 1
                        continue
                    fresh.append(r)
                t0 = time.perf_counter()
                texts = [prep_text(r["content"]) for r in fresh]
                summary["skipped_empty"] += texts.count(None)
                kept = [(r, t) for r, t in zip(fresh, texts) if t]
                if kept:
                    vecs = embedder.encode_passages([t for _, t in kept])
                    with idx:
                        for (r, t), v in zip(kept, vecs):
                            cur = idx.execute(
                                "INSERT INTO units(source, conversation_id, seq,"
                                " role, turn_key, src_id, time, title, text, chars)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (r["source"], r["conversation_id"], r["seq"],
                                 r["role"], r["turn_key"], r["src_id"], r["time"],
                                 r["title"], t, len(t)),
                            )
                            idx.execute(
                                f"INSERT INTO {spec.vec_table}(rowid, embedding)"
                                " VALUES(?,?)",
                                (cur.lastrowid,
                                 np.ascontiguousarray(v, dtype=np.float32).tobytes()),
                            )
                            known.add((r["source"], r["conversation_id"], r["seq"]))
                    summary["embedded"] += len(kept)
                summary["scanned"] += len(rows)
                summary["batches"] += 1
                wm = {
                    "source": rows[-1]["source"],
                    "conversation_id": rows[-1]["conversation_id"],
                    "seq": rows[-1]["seq"],
                    "at": int(time.time()),
                }
                # 必须显式提交：watermark 独立于 units 事务，
                # 若仅靠隐式事务将在进程退出时丢最后一批（回归测试锁定）
                with idx:
                    idx.execute(
                        "INSERT OR REPLACE INTO ingest_meta(key,value)"
                        " VALUES('watermark',?)",
                        (json.dumps(wm, ensure_ascii=False),),
                    )
                if limit is not None and summary["scanned"] >= limit:
                    log.info(
                        "ingest batch=%d scanned=%d embedded=%d skipped_known=%d "
                        "limit=%d reached",
                        summary["batches"], summary["scanned"], summary["embedded"],
                        summary["skipped_known"], limit,
                    )
                    break
                log.info(
                    "ingest batch=%d scanned=%d embedded=%d skipped_known=%d "
                    "batch_ms=%d watermark=%s/%s/%d",
                    summary["batches"], summary["scanned"], summary["embedded"],
                    summary["skipped_known"],
                    int((time.perf_counter() - t0) * 1000),
                    wm["source"], wm["conversation_id"], wm["seq"],
                )
        finally:
            src.close()
        gc = gc_vec_orphans(idx, spec.vec_table)
        if gc:
            log.info("vec orphan gc table=%s deleted=%d", spec.vec_table, gc)
        summary["vec_gc"] = gc
        summary["seconds"] = round(time.perf_counter() - t_all, 2)
        log.info(
            "ingest done model=%s scanned=%d embedded=%d skipped_known=%d "
            "skipped_empty=%d batches=%d secs=%.2f",
            summary["model"], summary["scanned"], summary["embedded"],
            summary["skipped_known"], summary["skipped_empty"],
            summary["batches"], summary["seconds"],
        )
        return summary
    finally:
        idx.close()


def run_reembed(
    settings: Settings,
    *,
    model_id: str | None = None,
    batch_size: int = 32,
    log: logging.Logger | None = None,
) -> dict:
    """模型切换路径：units(文本)不动，按新模型重建其专属向量表。"""
    log = log or logging.getLogger("asrag.ingest")
    spec = settings.model(model_id) if model_id else settings.active_spec
    embedder = OnnxEmbedder(spec, batch_size=batch_size, log=log)
    idx = open_index(settings.index_db)
    try:
        n_units = idx.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        drop_vec_table(idx, spec)
        ensure_vec_table(idx, spec)
        t0 = time.perf_counter()
        done = 0
        last_id = 0
        while True:
            rows = idx.execute(
                "SELECT id, text FROM units WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            vecs = embedder.encode_passages([r[1] for r in rows])
            with idx:
                for (uid, _), v in zip(rows, vecs):
                    idx.execute(
                        f"INSERT OR REPLACE INTO {spec.vec_table}(rowid, embedding)"
                        " VALUES(?,?)",
                        (uid, np.ascontiguousarray(v, dtype=np.float32).tobytes()),
                    )
            last_id = rows[-1][0]
            done += len(rows)
            log.info(
                "reembed model=%s progress=%d/%d last_unit_id=%d",
                spec.id, done, n_units, last_id,
            )
        idx.execute(
            "INSERT OR REPLACE INTO ingest_meta(key,value) VALUES(?,?)",
            (f"vecbuilt.{spec.vec_table}",
             json.dumps({"units": done, "at": int(time.time())})),
        )
        idx.commit()
        gc = gc_vec_orphans(idx, spec.vec_table)
        if gc:
            log.info("reembed vec orphan gc table=%s deleted=%d",
                     spec.vec_table, gc)
        secs = round(time.perf_counter() - t0, 2)
        log.info("reembed done model=%s units=%d secs=%.2f", spec.id, done, secs)
        return {"model": spec.id, "reembedded": done, "seconds": secs}
    finally:
        idx.close()


def run_stats(settings: Settings, log: logging.Logger | None = None) -> dict:
    """索引规模/水位统计（真实库验收依据）。"""
    spec = settings.active_spec
    idx = open_index(settings.index_db)
    try:
        out: dict = {"model": spec.id, "dim": spec.dim}
        out["units_total"] = idx.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        out["by_role"] = dict(idx.execute(
            "SELECT role, COUNT(*) FROM units GROUP BY role"))
        out["by_source"] = dict(idx.execute(
            "SELECT source, COUNT(*) FROM units GROUP BY source ORDER BY 2 DESC"))
        t = spec.vec_table
        exists = idx.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        out["vec_total"] = (
            idx.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if exists else 0
        )
        out["vec_coverage"] = (
            round(out["vec_total"] / out["units_total"], 4) if out["units_total"] else 0.0
        )
        wm = idx.execute(
            "SELECT value FROM ingest_meta WHERE key='watermark'"
        ).fetchone()
        out["watermark"] = json.loads(wm[0]) if wm else None
        return out
    finally:
        idx.close()
