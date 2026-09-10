"""混合召回：向量 + trigram 全文(正文/标题) + 精确标识符 → RRF 融合 → 多样性选择 → 轮次展开。

设计依据（docs/recall-roadmap.md，参照 MemOS core/retrieval 机制分析）：
- P0-1 标题通道：units_fts 含 title 列（标题权重 2.0），解"关键词只在标题"盲区；
- P0-2 标识符通道：高熵串（≥12 字符含分隔/数字）精确 LIKE，命中固定强分；
- P0-3 滑动窗口 + 噪音过滤：3 字滑窗（步长1）全词覆盖，纯噪音块剔除（MemOS CJK 噪音表思路）；
- P0-4 多样性：同会话限席 + 简化 MMR（用已存向量算冗余，零额外推理）。

溯源要求：查询文本、各路命中 id、融合结果 id、耗时全部入日志。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from .config import Settings
from .embedder import Embedder, OnnxEmbedder
from .ingest import open_index

# trigram 分词器要求查询串 ≥3 字符；更短走 LIKE 兜底
TRIGRAM_MIN_LEN = 3
CHUNK_CAP = 16
IDENT_CAP = 5
# MemOS keyword.ts:36 同源噪音表：纯疑问/代词块匹配海量文档，只会灌噪声
_CJK_NOISE = set("我你他她它的了呢吗么还记得是有想请问谁哪帮")
# 高熵标识符：长度≥12 且含 数字/_/-/:/.  或 纯小写长串≥16（timeout_seconds 类）
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-:.]{11,}")


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
    ident_rank: int | None = None
    turn_context: list[tuple[str, str]] = field(default_factory=list)


# ── FTS schema：懒建 + 单列→双列迁移 + 存量回填 + 触发器同步 ────────────────

def _fts_columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(units_fts)")]


def ensure_search_schema(conn: sqlite3.Connection, log: logging.Logger | None = None) -> None:
    """建立/校验 trigram FTS（text+title 双列）与触发器，回填存量。幂等，含旧版迁移。"""
    log = log or logging.getLogger("asrag.search")
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='units_fts'"
    ).fetchone()
    if has_fts and _fts_columns(conn) != ["text", "title"]:
        log.info("units_fts migrating to (text,title) columns")
        conn.executescript("""
            DROP TRIGGER IF EXISTS units_ai_fts;
            DROP TRIGGER IF EXISTS units_ad_fts;
            DROP TRIGGER IF EXISTS units_au_fts;
            DROP TABLE units_fts;
        """)
        has_fts = None
    if not has_fts:
        conn.execute(
            "CREATE VIRTUAL TABLE units_fts USING fts5("
            "text, title, tokenize='trigram')"
        )
        # 本机 SQLite 3.53.1 的 FTS5 特殊 'delete' 命令不可用（实测必报
        # SQL logic error）；普通 FTS5 表直接 DELETE rowid 即由 FTS5 自维护索引。
        conn.executescript("""
            CREATE TRIGGER units_ai_fts AFTER INSERT ON units BEGIN
                INSERT INTO units_fts(rowid, text, title)
                VALUES (new.id, new.text, IFNULL(new.title,''));
            END;
            CREATE TRIGGER units_ad_fts AFTER DELETE ON units BEGIN
                DELETE FROM units_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER units_au_fts AFTER UPDATE ON units BEGIN
                DELETE FROM units_fts WHERE rowid = old.id;
                INSERT INTO units_fts(rowid, text, title)
                VALUES (new.id, new.text, IFNULL(new.title,''));
            END;
        """)
        log.info("units_fts created (trigram, text+title, trigger-synced)")
    missing = conn.execute(
        "SELECT id, text, IFNULL(title,'') FROM units"
        " WHERE id NOT IN (SELECT rowid FROM units_fts)"
    ).fetchall()
    if missing:
        conn.executemany(
            "INSERT INTO units_fts(rowid, text, title) VALUES(?,?,?)", missing
        )
        conn.commit()
        log.info("units_fts backfilled rows=%d", len(missing))
    conn.commit()


# ── 查询分解 ────────────────────────────────────────────────────────────

def _fts_escape(term: str) -> str:
    return term.replace('"', '""')


def _query_chunks(query: str, cap: int = CHUNK_CAP) -> list[str]:
    """3 字滑动窗口（步长 1），剔除全噪音块；返回去重有序列表。"""
    out: list[str] = []
    seen: set[str] = set()
    for i in range(max(0, len(query) - TRIGRAM_MIN_LEN + 1)):
        chunk = query[i : i + TRIGRAM_MIN_LEN]
        if chunk in seen:
            continue
        if all(ch in _CJK_NOISE for ch in chunk):
            continue
        seen.add(chunk)
        out.append(chunk)
        if len(out) >= cap:
            break
    return out


def extract_identifiers(query: str) -> list[str]:
    """抽取高熵标识符（MemOS keyword.ts:29 思路）：含数字/分隔符的 ≥12 串，
    或纯小写 ≥16 长串（retry_handler_v2_max、timeout_seconds）。"""
    out: list[str] = []
    for m in _IDENT_RE.finditer(query):
        tok = m.group(0)
        strong = re.search(r"[0-9_\-:.]", tok) or len(tok) >= 16
        if strong and tok not in out:
            out.append(tok)
    return out[:IDENT_CAP]


# ── 三路检索 ────────────────────────────────────────────────────────────

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


def fts_search(
    conn: sqlite3.Connection, query: str, k: int
) -> list[tuple[int, float]]:
    """trigram 全文检索（正文+标题，标题权重 2.0）；<3 字符走 LIKE 兜底。
    返回 [(unit_id, bm25)]（bm25 越小越相关）。"""
    q = query.strip()
    if not q:
        return []
    chunks = _query_chunks(q) if len(q) >= TRIGRAM_MIN_LEN else []
    if chunks:
        match = " OR ".join(f'"{_fts_escape(c)}"' for c in chunks)
        rows = conn.execute(
            "SELECT rowid, bm25(units_fts, 1.0, 2.0) FROM units_fts"
            " WHERE units_fts MATCH ?"
            " ORDER BY bm25(units_fts, 1.0, 2.0) LIMIT ?",
            (match, k),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT u.id, 0 FROM units u"
            " WHERE u.text LIKE ? ESCAPE '\\' OR IFNULL(u.title,'') LIKE ? ESCAPE '\\'"
            " ORDER BY u.id LIMIT ?",
            ("%" + _like_esc(q) + "%", "%" + _like_esc(q) + "%", k),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _like_esc(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def identifier_search(
    conn: sqlite3.Connection, idents: list[str], k: int
) -> list[int]:
    """精确标识符通道：命中数降序、同数按 id 升序（确定性）。返回 unit_id 列表。"""
    if not idents:
        return []
    counts: dict[int, int] = {}
    for ident in idents:
        like = f"%{_like_esc(ident)}%"
        for (uid,) in conn.execute(
            "SELECT id FROM units"
            " WHERE text LIKE ? ESCAPE '\\' OR IFNULL(title,'') LIKE ? ESCAPE '\\'",
            (like, like),
        ).fetchall():
            counts[uid] = counts.get(uid, 0) + 1
    return [i for i, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:k]]


def rrf_fuse(
    ranked_lists: list[list[int]], *, k: int = 60
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：score = Σ 1/(k+rank)，rank 从 1 起。返回按分数降序。"""
    scores: dict[int, float] = {}
    for ranks in ranked_lists:
        for i, unit_id in enumerate(ranks):
            scores[unit_id] = scores.get(unit_id, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


# ── 多样性选择（P0-4：同会话限席 + 简化 MMR，向量已 L2 归一） ───────────────

def select_diverse(
    fused: list[tuple[int, float]],
    conv_of: dict[int, tuple[str, str]],
    emb_of: dict[int, np.ndarray],
    *,
    k: int,
    max_per_conversation: int = 2,
    lam: float = 0.7,
) -> list[tuple[int, float]]:
    """从融合池选 k 个：贪心 MMR（λ·rel − (1−λ)·maxCos），同 (source,conv) 限席。
    池空即返回不足额（无兜底注水）。"""
    if not fused:
        return []
    top_score = fused[0][1]
    pool = list(fused[: max(k * 4, k)])
    picked: list[tuple[int, float]] = []
    picked_embs: list[np.ndarray] = []
    per_conv: dict[tuple[str, str], int] = {}
    while len(picked) < k and pool:
        best_i, best_val = -1, float("-inf")
        for i, (uid, score) in enumerate(pool):
            conv = conv_of.get(uid)
            if conv and per_conv.get(conv, 0) >= max_per_conversation:
                continue
            rel = score / top_score if top_score > 0 else 0.0
            e = emb_of.get(uid)
            red = 0.0
            if e is not None and picked_embs:
                red = max(float(e @ pe) for pe in picked_embs)
            val = lam * rel - (1.0 - lam) * red
            if val > best_val:
                best_i, best_val = i, val
        if best_i < 0:
            break  # 剩余全部被限席
        uid, score = pool.pop(best_i)
        picked.append((uid, score))
        conv = conv_of.get(uid)
        if conv:
            per_conv[conv] = per_conv.get(conv, 0) + 1
        e = emb_of.get(uid)
        if e is not None:
            picked_embs.append(e)
    return picked


# ── 结果组装 ─────────────────────────────────────────────────────────────

def _fetch_units(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
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
    diversity: bool = True,
    max_per_conversation: int = 2,
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
        idents = extract_identifiers(query) if mode == "hybrid" else []
        ident_ids = identifier_search(conn, idents, candidate_k) if mode == "hybrid" else []
        if mode in ("hybrid", "vector"):
            qvec = embedder.encode_query(query)
            vec_ids = [i for i, _ in vector_search(conn, spec.vec_table, qvec, candidate_k)]
        if mode in ("hybrid", "fts"):
            fts_ids = [i for i, _ in fts_search(conn, query, candidate_k)]

        if mode == "hybrid":
            fused = rrf_fuse([lst for lst in (vec_ids, fts_ids, ident_ids) if lst])
        else:
            fused = [(i, 1.0) for i in (vec_ids if mode == "vector" else fts_ids)]

        if diversity and mode == "hybrid":
            pool = fused[: max(k * 4, k)]
            metas = _fetch_units(conn, [i for i, _ in pool])
            conv_of = {i: (m["source"], m["conversation_id"]) for i, m in metas.items()}
            emb_of: dict[int, np.ndarray] = {}
            if pool:
                marks = ",".join("?" * len(pool))
                for rid, blob in conn.execute(
                    f"SELECT rowid, embedding FROM {spec.vec_table}"
                    f" WHERE rowid IN ({marks})", [i for i, _ in pool]
                ):
                    emb_of[rid] = np.frombuffer(blob, dtype=np.float32)
            top = select_diverse(fused, conv_of, emb_of,
                                 k=k, max_per_conversation=max_per_conversation)
        else:
            top = fused[:k]

        by_id = _fetch_units(conn, [i for i, _ in top])
        vr = {i: r + 1 for r, i in enumerate(vec_ids)}
        fr = {i: r + 1 for r, i in enumerate(fts_ids)}
        ir = {i: r + 1 for r, i in enumerate(ident_ids)}
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
                ident_rank=ir.get(unit_id),
            )
            if expand_turns:
                hit.turn_context = expand_turn(
                    conn, hit.source, hit.conversation_id, hit.turn_key)
            hits.append(hit)
        log.info(
            "search q=%r mode=%s k=%d idents=%s vec_ids=%s fts_ids=%s ident_ids=%s"
            " fused_ids=%s cost_ms=%d",
            query, mode, k, idents, vec_ids[:15], fts_ids[:15], ident_ids[:10],
            [i for i, _ in top], int((time.perf_counter() - t0) * 1000),
        )
        return hits
    finally:
        conn.close()
