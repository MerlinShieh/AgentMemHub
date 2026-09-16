"""混合召回（P1 结构，参照 MemOS core/retrieval 机制分析）：

候选级通道融合：
    relevance = best_channel_score          # 各通道拉到同一 (0,1] 尺度取最好
              + RRF_WEIGHT · Σ 1/(RRF_K+rank)   # 多通道一致命中的投票 lift
    → 价值偏置（可选 ValueProvider，≤0.3 有界 + 30d 半衰期，P2-2 外置）
    → 相对阈值 ×floor（多通道强信号可 bypass）
    → 会话限席 + MMR（P0-4）
    → 终审（可选 Judge，P2-1 外置；fail-closed 归调用方约定）

通道：vec（余弦相似=1-距离）、fts（trigram 正文+标题，名次倒数）、
ident（高熵串精确匹配，强证据恒 1.0）。
溯源：查询、三路 id、旁路/过滤计数、耗时全部入日志。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Sequence

import numpy as np

from .config import Settings
from .embedder import Embedder, OnnxEmbedder
from .runtime import get_embedder
from .ingest import ensure_vec_table, open_index

if TYPE_CHECKING:
    from .ext import Judge, ValueProvider

TRIGRAM_MIN_LEN = 3
CHUNK_CAP = 16
IDENT_CAP = 5
RRF_K = 60
RRF_WEIGHT = 0.4
THRESHOLD_FLOOR = 0.2      # MemOS 同源默认（ranker.ts:120）
STRONG_BYPASS_SCORE = 0.35  # bypass 需 ≥2 通道且其中最好通道分达此线
# MemOS keyword.ts:36 同源噪音表
_CJK_NOISE = set("我你他她它的了呢吗么还记得是有想请问谁哪帮")
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-:.]{11,}")

ChannelHits = list[tuple[int, float]]  # [(unit_id, score 越大越好)]


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
    bypassed: bool = False
    turn_context: list[tuple[str, str]] = field(default_factory=list)


# ── 候选级融合与闸门（纯函数，直接断言） ─────────────────────────────────

def fuse_channels(channels: dict[str, ChannelHits]) -> dict[int, dict[str, tuple[int, float]]]:
    """{channel: [(id,score)]} → {unit_id: {channel: (rank1起, score)}}"""
    cand: dict[int, dict[str, tuple[int, float]]] = {}
    for name, hits in channels.items():
        for rank, (uid, score) in enumerate(hits, 1):
            cand.setdefault(uid, {})[name] = (rank, score)
    return cand


def relevance_of(entry: dict[str, tuple[int, float]],
                 *, rrf_weight: float = RRF_WEIGHT, rrf_k: int = RRF_K) -> float:
    best = max(s for _, s in entry.values())
    rrf = sum(1.0 / (rrf_k + rank) for rank, _ in entry.values())
    return best + rrf_weight * rrf


def threshold_filter(
    rel: dict[int, float], cand: dict[int, dict[str, tuple[int, float]]],
    *, floor: float = THRESHOLD_FLOOR, strong: float = STRONG_BYPASS_SCORE,
) -> tuple[dict[int, float], set[int]]:
    """相对阈值：rel < floor×top 剔除；≥2 通道且最好通道分 ≥ strong 可旁路
    （防"纯关键词 rank-0 命中被 cosine 尺度绞杀"，MemOS ranker.ts:462 同源思想）。
    返回 (存活, 旁路id集)。"""
    if not rel:
        return {}, set()
    top = max(rel.values())
    keep: dict[int, float] = {}
    bypassed: set[int] = set()
    for uid, r in rel.items():
        entry = cand[uid]
        best = max(s for _, s in entry.values())
        if r >= floor * top:
            keep[uid] = r
        elif len(entry) >= 2 and best >= strong:
            keep[uid] = r
            bypassed.add(uid)
    return keep, bypassed


def rank_by_relevance(rel: dict[int, float]) -> list[tuple[int, float]]:
    return sorted(rel.items(), key=lambda x: (-x[1], x[0]))


# ── FTS schema（懒建/迁移/回填/触发器） ─────────────────────────────────

def _fts_columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(units_fts)")]


def ensure_search_schema(conn: sqlite3.Connection, log: logging.Logger | None = None) -> None:
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
        # 本机 SQLite 3.53.1 的 FTS5 特殊 'delete' 命令不可用；
        # 普通 FTS5 表直接 DELETE rowid 由 FTS5 自维护索引（实测坑）。
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
            "INSERT INTO units_fts(rowid, text, title) VALUES(?,?,?)", missing)
        conn.commit()
        log.info("units_fts backfilled rows=%d", len(missing))
    conn.commit()


# ── 查询分解 ────────────────────────────────────────────────────────────

def _fts_escape(term: str) -> str:
    return term.replace('"', '""')


def _like_esc(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_chunks(query: str, cap: int = CHUNK_CAP) -> list[str]:
    """3 字滑动窗口（步长 1），全噪音块剔除、去重有序。"""
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
    """高熵标识符：≥12 且含 数字/_-:. 分隔，或纯字母数字长串 ≥16。"""
    out: list[str] = []
    for m in _IDENT_RE.finditer(query):
        tok = m.group(0)
        if re.search(r"[0-9_\-:.]", tok) or len(tok) >= 16:
            if tok not in out:
                out.append(tok)
    return out[:IDENT_CAP]


# ── 三路通道（均支持 exclude_session） ─────────────────────────────────

def _excl_ids(conn: sqlite3.Connection, exclude: tuple[str, str] | None) -> set[int]:
    if not exclude:
        return set()
    return {r[0] for r in conn.execute(
        "SELECT id FROM units WHERE source=? AND conversation_id=?", exclude)}


def vector_search(conn: sqlite3.Connection, vec_table: str, qvec: np.ndarray,
                  k: int, *, exclude_ids: set[int] | None = None) -> ChannelHits:
    """余弦相似 = 1-距离，裁剪到 (0,1]。exclude 通过超采样后滤除近似实现。"""
    fetch = k * 3 if exclude_ids else k
    rows = conn.execute(
        f"SELECT rowid, distance FROM {vec_table}"
        " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qvec.astype(np.float32).tobytes(), fetch),
    ).fetchall()
    out: ChannelHits = []
    for rid, dist in rows:
        if exclude_ids and rid in exclude_ids:
            continue
        out.append((rid, max(1.0 - float(dist), 0.0)))
        if len(out) >= k:
            break
    return out


def fts_search(conn: sqlite3.Connection, query: str, k: int,
               *, exclude_ids: set[int] | None = None) -> ChannelHits:
    q = query.strip()
    if not q:
        return []
    excl = exclude_ids or set()
    chunks = _query_chunks(q) if len(q) >= TRIGRAM_MIN_LEN else []
    if chunks:
        match = " OR ".join(f'"{_fts_escape(c)}"' for c in chunks)
        rows = conn.execute(
            "SELECT rowid, bm25(units_fts, 1.0, 2.0) FROM units_fts"
            " WHERE units_fts MATCH ?"
            " ORDER BY bm25(units_fts, 1.0, 2.0) LIMIT ?",
            (match, k + len(excl)),
        ).fetchall()
        scored = [(r, 1.0 / (i + 1)) for i, (r, _bm) in enumerate(rows)
                  if r not in excl][:k]
    else:
        like = f"%{_like_esc(q)}%"
        rows = conn.execute(
            "SELECT u.id FROM units u"
            " WHERE (u.text LIKE ? ESCAPE '\\' OR IFNULL(u.title,'') LIKE ? ESCAPE '\\')"
            " ORDER BY u.id LIMIT ?",
            (like, like, k + len(excl)),
        ).fetchall()
        scored = [(r[0], 1.0) for r in rows if r[0] not in excl][:k]
    return scored


def identifier_search(conn: sqlite3.Connection, idents: list[str], k: int,
                      *, exclude_ids: set[int] | None = None) -> ChannelHits:
    if not idents:
        return []
    excl = exclude_ids or set()
    counts: dict[int, int] = {}
    for ident in idents:
        like = f"%{_like_esc(ident)}%"
        for (uid,) in conn.execute(
            "SELECT id FROM units"
            " WHERE text LIKE ? ESCAPE '\\' OR IFNULL(title,'') LIKE ? ESCAPE '\\'",
            (like, like),
        ).fetchall():
            if uid not in excl:
                counts[uid] = counts.get(uid, 0) + 1
    ordered = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:k]
    return [(uid, 1.0) for uid, _ in ordered]  # 精确匹配恒强证据


# ── 多样性选择（P0-4） ─────────────────────────────────────────────────

def select_diverse(fused: list[tuple[int, float]],
                   conv_of: dict[int, tuple[str, str]],
                   emb_of: dict[int, np.ndarray], *,
                   k: int, max_per_conversation: int = 2,
                   lam: float = 0.7) -> list[tuple[int, float]]:
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
            break
        uid, score = pool.pop(best_i)
        picked.append((uid, score))
        conv = conv_of.get(uid)
        if conv:
            per_conv[conv] = per_conv.get(conv, 0) + 1
        e = emb_of.get(uid)
        if e is not None:
            picked_embs.append(e)
    return picked


# ── 结果组装 ────────────────────────────────────────────────────────────

def _fetch_units(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, source, conversation_id, seq, role, turn_key, time, title, text"
        f" FROM units WHERE id IN ({marks})", ids)
    return {r["id"]: r for r in rows}


def expand_turn(conn: sqlite3.Connection, source: str, conversation_id: str,
                turn_key: str | None) -> list[tuple[str, str]]:
    if not turn_key:
        return []
    rows = conn.execute(
        "SELECT role, text FROM units"
        " WHERE source=? AND conversation_id=? AND turn_key=? ORDER BY seq",
        (source, conversation_id, turn_key)).fetchall()
    return [(r["role"], r["text"]) for r in rows]


def hybrid_search(
    settings: Settings,
    query: str,
    *,
    embedder: Embedder | None = None,
    k: int = 10,
    candidate_k: int = 30,
    expand_turns: bool = True,
    mode: str = "hybrid",            # hybrid | vector | fts
    diversity: bool = True,
    max_per_conversation: int = 2,
    exclude_session: tuple[str, str] | None = None,   # P1-3 (source, conv_id)
    value_provider: "ValueProvider | None" = None,    # P2-2 读侧价值 join
    no_decay_ids: "Callable[[Sequence[int]], set[int]] | None" = None,
    # 手动加权的 unit 集合查询器：锁定的价值不随时间衰减
    include_low_value: bool = False,                  # 复盘模式放开 value<=0
    judge: "Judge | None" = None,                     # P2-1 终审
    threshold_floor: float = THRESHOLD_FLOOR,
    log: logging.Logger | None = None,
) -> list[Hit]:
    log = log or logging.getLogger("asrag.search")
    spec = settings.active_spec
    t0 = time.perf_counter()
    conn = open_index(settings.index_db)
    conn.row_factory = sqlite3.Row
    embedder = embedder or get_embedder(spec, settings=settings)
    try:
        ensure_search_schema(conn, log=log)
        excl = _excl_ids(conn, exclude_session)
        idents = extract_identifiers(query) if mode == "hybrid" else []

        channels: dict[str, ChannelHits] = {}
        if mode in ("hybrid", "vector"):
            # 多模型向量路：按 rag.retrieval.models 逐模型检索（配置语义落地）。
            # active 模型通道名保持 "vec"（兼容 Hit.vec_rank 与既有行为），
            # 其余模型用 "vec:<model_id>" —— 各路独立参与 RRF，一致命中信号更强。
            for mid in settings.retrieval_models:
                s = settings.model(mid)
                ensure_vec_table(conn, s)
                em = embedder if mid == spec.id else get_embedder(s, settings=settings)
                qv = em.encode_query(query)
                name = "vec" if mid == spec.id else f"vec:{mid}"
                channels[name] = vector_search(conn, s.vec_table, qv,
                                               candidate_k, exclude_ids=excl)
        if mode in ("hybrid", "fts"):
            channels["fts"] = fts_search(conn, query, candidate_k, exclude_ids=excl)
        if mode == "hybrid" and idents:
            channels["ident"] = identifier_search(conn, idents, candidate_k,
                                                  exclude_ids=excl)

        cand = fuse_channels(channels)
        rel = {uid: relevance_of(e) for uid, e in cand.items()}

        dropped_low: list[int] = []
        if value_provider is not None and rel:
            from .ext import apply_value_boost

            meta = {uid: (m["time"],) for uid, m in
                    _fetch_units(conn, list(rel)).items()}
            values = value_provider.values(list(rel))
            rel, dropped_low = apply_value_boost(
                rel, meta, values, include_low_value=include_low_value,
                no_decay=(no_decay_ids(list(rel)) if no_decay_ids else None))

        if mode == "hybrid" and rel:
            rel, bypassed = threshold_filter(rel, cand, floor=threshold_floor)
        else:
            bypassed = set()

        ranked = rank_by_relevance(rel)
        if diversity:
            pool = ranked[: max(k * 4, k)]
            metas = _fetch_units(conn, [i for i, _ in pool])
            # 会话限席只约束会话轨迹；原子记忆（source='memory'）同属伪会话
            # (memory/mcp)，每条独立成席，否则一批记忆互相挤占 top-k
            conv_of = {i: (m["source"],
                           f"u{i}" if m["source"] == "memory"
                           else m["conversation_id"])
                       for i, m in metas.items()}
            emb_of: dict[int, np.ndarray] = {}
            if pool:
                # MMR 仅用 active 模型的向量（维度一致；多模型混合会维度冲突），
                # 缺向量/表未建时该条不参与多样性计算（不影响召回本身）。
                try:
                    marks = ",".join("?" * len(pool))
                    for rid, blob in conn.execute(
                        f"SELECT rowid, embedding FROM {spec.vec_table}"
                        f" WHERE rowid IN ({marks})", [i for i, _ in pool]):
                        emb_of[rid] = np.frombuffer(blob, dtype=np.float32)
                except sqlite3.OperationalError:
                    pass    # active 向量表尚未建立（换模型首次写入前）→ 退化为无向量去重
            top = select_diverse(ranked, conv_of, emb_of,
                                 k=k, max_per_conversation=max_per_conversation)
        else:
            top = ranked[:k]

        by_id = _fetch_units(conn, [i for i, _ in top])
        first_rank = {ch: {uid: i for i, (uid, _) in enumerate(hits, 1)}
                      for ch, hits in channels.items()}
        hits: list[Hit] = []
        for unit_id, score in top:
            r = by_id.get(unit_id)
            if r is None:
                continue
            entry = cand.get(unit_id, {})
            hit = Hit(
                unit_id=unit_id, source=r["source"],
                conversation_id=r["conversation_id"], seq=r["seq"],
                role=r["role"], turn_key=r["turn_key"], time=r["time"],
                title=r["title"], text=r["text"], score=score,
                vec_rank=entry.get("vec", (None,))[0],
                fts_rank=entry.get("fts", (None,))[0],
                ident_rank=entry.get("ident", (None,))[0],
                bypassed=unit_id in bypassed,
            )
            if expand_turns:
                hit.turn_context = expand_turn(
                    conn, hit.source, hit.conversation_id, hit.turn_key)
            hits.append(hit)

        if judge is not None:
            before = len(hits)
            hits = judge.filter(query, hits)
            log.info("judge applied in=%d out=%d", before, len(hits))

        log.info(
            "search q=%r mode=%s k=%d idents=%s vec_n=%d fts_n=%d ident_n=%d"
            " dropped_low=%d bypassed=%d excl=%d fused_ids=%s cost_ms=%d",
            query, mode, k, idents, len(channels.get("vec", [])),
            len(channels.get("fts", [])), len(channels.get("ident", [])),
            len(dropped_low), len(bypassed), len(excl),
            [i for i, _ in top], int((time.perf_counter() - t0) * 1000))
        return hits
    finally:
        conn.close()
