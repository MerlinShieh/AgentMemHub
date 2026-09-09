"""混合召回：向量 top-k（sqlite-vec）+ 中文 trigram 全文（FTS5）→ RRF 融合 → 轮次展开。

溯源要求：查询文本、向量命中 id、FTS 命中 id、融合结果 id、耗时全部入日志。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from .config import Settings
from .embedder import Embedder, OnnxEmbedder
from .ingest import open_index

# trigram 分词器要求查询串 ≥3 字符；更短走 LIKE 兜底
TRIGRAM_MIN_LEN = 3
FTS_SCHEMA_KEY = "fts_ready"


@dataclass
class Hit:
    unit_id: int
    source: str
    conversation_id: str
    seq: int
    role: str
    turn_key: str | None
    time: int | None
    title: str | None
    text: str
    score: float
    vec_rank: int | None = None
    fts_rank: int | None = None
    turn_context: list[tuple[str, str]] = field(default_factory=list)


# ── FTS schema：懒建 + 存量回填 + 触发器同步（对正在运行的 ingest 生效）────

def ensure_search_schema(conn: sqlite3.Connection, log: logging.Logger | None = None) -> None:
    """建立/校验 trigram FTS 与 units 触发器，并把已有 units 回填进 FTS。幂等。"""
    log = log or logging.getLogger("asrag.search")
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='units_fts'"
    ).fetchone()
    if not has_fts:
        conn.execute(
            "CREATE VIRTUAL TABLE units_fts USING fts5("
            "text, tokenize='trigram')"
        )
        # 注意：本机 SQLite 3.53.1 的 FTS5 特殊 'delete' 命令不可用
        # （实测必报 SQL logic error）；普通 FTS5 表直接 DELETE rowid
        # 即由 FTS5 自行维护索引，见 AGENTS.md 坑位记录。
        conn.executescript("""
            CREATE TRIGGER units_ai_fts AFTER INSERT ON units BEGIN
                INSERT INTO units_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER units_ad_fts AFTER DELETE ON units BEGIN
                DELETE FROM units_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER units_au_fts AFTER UPDATE ON units BEGIN
                DELETE FROM units_fts WHERE rowid = old.id;
                INSERT INTO units_fts(rowid, text) VALUES (new.id, new.text);
            END;
        """)
        log.info("units_fts created (trigram, trigger-synced)")
    # 存量回填（触发器只对未来写入生效；已入库 units 一次性补齐）
    missing = conn.execute(
        "SELECT id, text FROM units WHERE id NOT IN (SELECT rowid FROM units_fts)"
    ).fetchall()
    if missing:
        conn.executemany(
            "INSERT INTO units_fts(rowid, text) VALUES(?,?)", missing
        )
        conn.commit()
        log.info("units_fts backfilled rows=%d", len(missing))
    conn.commit()


# ── 两路检索 ─────────────────────────────────────────────────────────────

def vector_search(
    conn: sqlite3.Connection, vec_table: str, qvec: np.ndarray, k: int
) -> list[tuple[int, float]]:
    """返回 [(unit_id, cosine_distance)]，按距离升序。"""
    rows = conn.execute(
        f"SELECT rowid, distance FROM {vec_table}"
        " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qvec.astype(np.float32).tobytes(), k),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _fts_escape(term: str) -> str:
    return term.replace('"', '""')


def _query_chunks(query: str, size: int = 3, cap: int = 12) -> list[str]:
    """自然语言查询切成连续 3 字窗口（整句短语对 trigram 几乎不命中）。
    不足 size 的尾巴丢弃；返回去重后的块列表。"""
    out: list[str] = []
    for i in range(0, len(query) - size + 1, size):
        chunk = query[i : i + size]
        if chunk not in out:
            out.append(chunk)
    return out[:cap]


def fts_search(
    conn: sqlite3.Connection, query: str, k: int
) -> list[tuple[int, float]]:
    """trigram 全文检索（3 字窗口 OR）；<3 字符查询走 LIKE 兜底。
    返回 [(unit_id, bm25)]（bm25 越小越相关）。"""
    q = query.strip()
    if not q:
        return []
    chunks = _query_chunks(q) if len(q) >= TRIGRAM_MIN_LEN else []
    if chunks:
        match = " OR ".join(f'"{_fts_escape(c)}"' for c in chunks)
        rows = conn.execute(
            "SELECT rowid, bm25(units_fts) FROM units_fts"
            " WHERE units_fts MATCH ? ORDER BY bm25(units_fts) LIMIT ?",
            (match, k),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT u.id, 0 FROM units u"
            " WHERE u.text LIKE ? ESCAPE '\\' LIMIT ?",
            ("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", k),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def rrf_fuse(
    ranked_lists: list[list[int]], *, k: int = 60
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：score = Σ 1/(k+rank)，rank 从 1 起。返回按分数降序。"""
    scores: dict[int, float] = {}
    for ranks in ranked_lists:
        for i, unit_id in enumerate(ranks):
            scores[unit_id] = scores.get(unit_id, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


# ── 结果组装 ─────────────────────────────────────────────────────────────

def _fetch_units(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, source, conversation_id, seq, role, turn_key, time, title, text"
        f" FROM units WHERE id IN ({marks})", ids
    )
    return {r["id"]: r for r in rows}


def expand_turn(
    conn: sqlite3.Connection, source: str, conversation_id: str, turn_key: str | None
) -> list[tuple[str, str]]:
    """同轮上下文展开（(role, text) 按 seq 升序）。turn_key 空则不展开。"""
    if not turn_key:
        return []
    rows = conn.execute(
        "SELECT role, text FROM units"
        " WHERE source=? AND conversation_id=? AND turn_key=?"
        " ORDER BY seq",
        (source, conversation_id, turn_key),
    ).fetchall()
    return [(r["role"], r["text"]) for r in rows]


def hybrid_search(
    settings: Settings,
    query: str,
    *,
    embedder: Embedder | None = None,
    k: int = 10,
    candidate_k: int = 30,
    expand_turns: bool = True,
    mode: str = "hybrid",           # hybrid | vector | fts
    log: logging.Logger | None = None,
) -> list[Hit]:
    log = log or logging.getLogger("asrag.search")
    spec = settings.active_spec
    t0 = time.perf_counter()
    conn = open_index(settings.index_db)
    conn.row_factory = sqlite3.Row
    embedder = embedder or OnnxEmbedder(spec, log=log)
    try:
        ensure_search_schema(conn, log=log)
        vec_ids: list[int] = []
        fts_ids: list[int] = []
        if mode in ("hybrid", "vector"):
            qvec = embedder.encode_query(query)
            vec_hits = vector_search(conn, spec.vec_table, qvec, candidate_k)
            vec_ids = [i for i, _ in vec_hits]
        if mode in ("hybrid", "fts"):
            fts_ids = [i for i, _ in fts_search(conn, query, candidate_k)]

        lists = [lst for lst in (vec_ids, fts_ids) if lst]
        fused = rrf_fuse(lists) if mode == "hybrid" else [
            (i, 1.0) for i in (vec_ids if mode == "vector" else fts_ids)
        ]
        top = fused[:k]
        by_id = _fetch_units(conn, [i for i, _ in top]) if top else {}
        vr = {i: r + 1 for r, i in enumerate(vec_ids)}
        fr = {i: r + 1 for r, i in enumerate(fts_ids)}
        hits: list[Hit] = []
        for unit_id, score in top:
            r = by_id.get(unit_id)
            if r is None:
                continue
            hit = Hit(
                unit_id=unit_id, source=r["source"],
                conversation_id=r["conversation_id"], seq=r["seq"],
                role=r["role"], turn_key=r["turn_key"], time=r["time"],
                title=r["title"], text=r["text"], score=score,
                vec_rank=vr.get(unit_id), fts_rank=fr.get(unit_id),
            )
            if expand_turns:
                hit.turn_context = expand_turn(
                    conn, hit.source, hit.conversation_id, hit.turn_key)
            hits.append(hit)
        log.info(
            "search q=%r mode=%s k=%d vec_ids=%s fts_ids=%s fused_ids=%s cost_ms=%d",
            query, mode, k, vec_ids[:15], fts_ids[:15],
            [i for i, _ in top], int((time.perf_counter() - t0) * 1000),
        )
        return hits
    finally:
        conn.close()
