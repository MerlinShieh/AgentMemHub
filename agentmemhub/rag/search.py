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

from .config import DEFAULT_PAGE_POLICY, DEFAULT_RETRIEVAL, Settings
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
#: **多词短语**：2 个以上连续 ASCII 词（"command code" / "api key"）——见
#: extract_phrases 的说明（这是 identifier 通道覆盖不到的盲区）。
_PHRASE_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.\-]*(?:[ \t]+[A-Za-z][A-Za-z0-9_.\-]*)+")
#: 短语最短长度（"is a"、"a b" 这类短组合做字面匹配会引入海量噪声）
_PHRASE_MIN_LEN = 5
#: 短语上限（防查询串拼接；2 词窗口优先，见 extract_phrases）
_PHRASE_CAP = 4

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
    #: 命中的知识层级：page（L2 知识页，聚合答案）/ memory（蒸馏或直写记忆，
    #: 具体结论）/ message（原始对话细节）。调用方据此决定如何呈现——
    #: 页面给"标题+摘要+路径"（两阶段：需要时按 wiki_path 读全文），
    #: 记忆与消息本身就是短文本。
    kind: str = "message"
    #: 页面层专用：知识库目录里的相对路径（两阶段召回的第二阶段入口）
    wiki_path: str | None = None
    #: 页面层的摘要（从正文解析），比整页短、适合直接进上下文
    summary: str | None = None
    #: **数据来源维度**（与 kind 的层级维度正交）：native=自有记忆沉淀，
    #: external=外部投喂。Agent 与用户据此分辨知识出处（谁说的、可信度语境）。
    origin: str = "native"
    #: 命中的通道名集合（如 {"vec", "page_fts"}）。两个用途：页面准入判定
    #: "有没有跨池可比的强信号"（见 apply_page_policy）；排查"这条为何被召回"。
    signals: frozenset[str] = field(default_factory=frozenset)


#: 页面层召回时返回的正文上限（字符）——整页中位 1639、最长 7221，
#: 全塞进上下文会挤占空间；需要细节时按 wiki_path 读全文。
PAGE_TEXT_CAP = 600

#: 页面独立通道取前 N（内置默认值；可由 `rag.retrieval.page.channel_k` 覆盖，
#: 生效值走 settings.page_policy）
PAGE_CHANNEL_K = DEFAULT_PAGE_POLICY["channel_k"]


def kind_of(src_id: str | None) -> str:
    """按投影锚判定知识层级（锚前缀是唯一可靠的判据）。"""
    s = src_id or ""
    if s.startswith("wiki_"):
        return "page"
    if s.startswith(("dst_", "mcp_")):
        return "memory"
    return "message"


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
    exempt_ids: set[int] | None = None,
) -> tuple[dict[int, float], set[int]]:
    """相对阈值：rel < floor×top 剔除；≥2 通道且最好通道分 ≥ strong 可旁路
    （防"纯关键词 rank-0 命中被 cosine 尺度绞杀"，MemOS ranker.ts:462 同源思想）。
    返回 (存活, 旁路id集)。

    ``exempt_ids``（页面）不参与本阈值：页面的 rel 分天然低一个量级
    （实测 0.014~0.03，因为长文本聚合分被压制），0.2×top 对它们是系统性误杀——
    实测拼错查询 "windowsctrol" 下，两条**有字面证据**的真相关页面
    （rel≈0.19）正好卡在这条线外被剔掉。页面已有自己的准入策略
    （配额 + 证据排序，见 ``apply_page_policy``），职责不重复叠加。
    """
    if not rel:
        return {}, set()
    top = max(rel.values())
    keep: dict[int, float] = {}
    bypassed: set[int] = set()
    for uid, r in rel.items():
        if exempt_ids and uid in exempt_ids:
            keep[uid] = r
            continue
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


#: 页面命中的**证据等级**（越小越优先）。判据是"这个信号是否跨池可比"：
#:
#: · ``literal`` 字面证据（phrase 短语整串 / ident 标识符 / page_fts 页面词面 /
#:   fts 全局全文）：查询词确实出现在内容里，可信度最高；
#: · ``vector`` 全局向量池（vec / vec:<model>）：语义邻近，但含偶然——实测
#:   噪声「Clink」正是靠它混进来的；
#: · ``pool`` 仅页面池 KNN（page）：池子只有几百条，对任何查询都能凑出 top-k，
#:   单独出现几乎不构成证据，只能作为最后候选。
_PAGE_LITERAL_SIGNALS = frozenset({"phrase", "ident", "page_fts", "fts"})
_PAGE_EVIDENCE_ORDER = {"literal": 0, "vector": 1, "pool": 2}


def page_evidence(cand_entry: dict | None) -> str:
    """判定一条页面命中的证据等级（见 ``_PAGE_EVIDENCE_ORDER`` 的说明）。"""
    sigs = set(cand_entry or {})
    if sigs & _PAGE_LITERAL_SIGNALS:
        return "literal"
    if "vec" in sigs or any(s.startswith("vec:") for s in sigs):
        return "vector"
    return "pool"


def apply_page_policy(
    ranked: list[tuple[int, float]],
    cand: dict[int, dict[str, tuple[int, float]]],
    page_ids: set[int],
    *,
    policy: dict | None = None,
) -> tuple[list[tuple[int, float]], list[int]]:
    """页面层准入：**配额约束下的择优**——证据等级只影响排序，不剔除页面。

    两轮实测（231 页 × 十余个真实查询）得出了这条规则的最终形态：

    **① 分数不能用来判别**：页面融合分与相关性甚至反相关——"LLM Wiki 工程化"
    页（词面命中第 1、真相关）只有 0.21×top，而完全无关的「Mobile_App_AutoTest
    发布记录」有 0.63×top。页面作为长文本聚合产物，融合分被"短条目天然高分 +
    价值加权"系统性压制，与记忆/消息不可比。

    **② 证据也不能当门槛**（第一版踩的坑）：改成"无强信号即剔除"后，查询
    "windowsctrol"（拼错）的结果同时做错了两件事——语义最相关的「Windows
    Control Core 窗口控制内核」只有弱信号（page）被**判死**，而噪声「Clink」
    因偶然进入全局向量池被**放行**。拼错、换词、近义表达时，"真相关"恰恰最
    缺字面证据；用证据做门槛等于系统性惩罚这些场景。

    **所以本函数做的是"高分优先 + 低分字面兜底"**：

    ① 候选按**分数降序**——高分语义相关优先。实测 "github网络失败"：真正对症的
       「网络环境确认与连接故障排查」0.848、「opencode 卡在网络故障」0.841 若按
       证据等级排序，会输给 0.334/0.197 两条"只字面沾边"的页面；
    ② **低分页面必须有字面证据**（低于 ``literal_required_below``）才算数——
       拼错场景真相关页只有 0.014~0.026 分，靠的正是残缺的 `page_fts`；
    ③ 给这类"低分模糊匹配"留 ``literal_seats`` 个**保底席位**，否则中分的池内页
       （拼错查询里的「Clink」0.627）会按分数把它们全部挤出配额。

    可选收紧：``min_evidence``（``literal`` / ``vector`` / ``pool`` / ``any``）
    是证据硬门槛，默认 ``any``——**档位表刻意不使用它**：它是"对所有页面生效"
    的门槛，会把高分页面一起挡掉（实测档 3 曾用它，结果把候选里分最高的
    「网络环境确认与连接故障排查」0.668 挡在门外，而 0.334/0.197 两条低分字面
    页入选）。``floor_ratio`` 默认 0。

    返回 ``(存活列表, 被剔除的页面 id)``；非页面条目永不受本策略影响。
    """
    p = {**DEFAULT_PAGE_POLICY, **(policy or {})}
    if not ranked or not page_ids:
        return list(ranked), []
    top = ranked[0][1]
    floor = top * float(p.get("floor_ratio", 0.0) or 0.0)
    quota = max(int(p.get("max_in_results", 3)), 0)
    literal_seats = min(max(int(p.get("literal_seats", 1)), 0), quota)
    weak_below = float(p.get("literal_required_below", 0.7))
    limit = {"any": 2, "pool": 2, "vector": 1, "literal": 0}.get(
        str(p.get("min_evidence", "any") or "any").lower(), 2)

    candidates = [(uid, score) for uid, score in ranked
                  if uid in page_ids and score >= floor
                  and _PAGE_EVIDENCE_ORDER[page_evidence(cand.get(uid))] <= limit]
    candidates.sort(key=lambda x: -x[1])          # 分数优先（高分语义相关在前）
    high = [uid for uid, s in candidates if s >= weak_below]
    low_literal = [uid for uid, s in candidates
                   if s < weak_below and page_evidence(cand.get(uid)) == "literal"]

    picked: list[int] = []
    seen: set[int] = set()

    def take(lst: list[int], cap: int) -> None:
        for uid in lst:
            if len(picked) >= cap:
                return
            if uid in seen:
                continue
            picked.append(uid)
            seen.add(uid)

    take(high, quota - literal_seats)   # ① 高分语义，给字面兜底留出席位
    take(low_literal, quota)            # ② 低分但有字面证据的模糊匹配
    take(high, quota)                   # ③ 某一类不足时另一类补齐
    admitted = set(picked)

    kept: list[tuple[int, float]] = []
    dropped: list[int] = []
    for uid, score in ranked:
        if uid in page_ids and uid not in admitted:
            dropped.append(uid)
        else:
            kept.append((uid, score))
    return kept, dropped


def reserve_pages(
    top: list[tuple[int, float]],
    ranked: list[tuple[int, float]],
    page_ids: set[int],
    *,
    policy: dict | None = None,
) -> list[tuple[int, float]]:
    """给**已准入**的页面保底占位，防止被记忆/消息挤出最终 k 条。

    为什么必须保位：相关页的融合分可能只有 0.21×top（"LLM Wiki 工程化"页
    对记忆 1.007），纯按分数排序根本进不了前 k——可它是词面精确命中第 1 名的
    聚合答案。页面通道的立身之本就是"异质候选源各自成路"，这条原则要贯彻到
    最终排序，否则准入做得再准也白搭（保下来又挤出去）。

    **占位方式：替换末尾的非页面条目，而不是追加**——总数必须保持 ≤ k。
    实测踩过：追加会让 `len(hits) = k + 1`，而下游（`rag_bridge.safe_cutoff_hits`
    的 `hits[:max_keep]` 窗口、面板 `/api/memos/search` 的 `top` 截断）都按
    前 N 条切，排在末尾的页面**正好被切掉**（k=8 时页面在窗口内所以正常，
    k=20 时页面掉出窗口就消失了）。

    `ranked` 应为**准入过滤之后**的列表；返回结果保持分数降序。
    """
    p = {**DEFAULT_PAGE_POLICY, **(policy or {})}
    if not p.get("reserve_seats", True):
        return list(top)
    quota = max(int(p.get("max_in_results", 3)), 0)
    if not quota:
        return list(top)
    chosen = {u for u, _ in top}
    room = quota - sum(1 for u, _ in top if u in page_ids)
    if room <= 0:
        return list(top)
    reserve = [(u, s) for u, s in ranked
               if u in page_ids and u not in chosen][:room]
    if not reserve:
        return list(top)
    out = list(top)
    for uid, score in reserve:
        for i in range(len(out) - 1, -1, -1):      # 从末尾找一个非页面替换
            if out[i][0] not in page_ids:
                out[i] = (uid, score)
                break
        else:
            out.append((uid, score))               # 全是被保页面时空位不足才追加
    return sorted(out, key=lambda x: -x[1])


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


def extract_phrases(query: str) -> list[str]:
    """提取查询中的**多词短语**（英文词组，如 "command code" / "api key"）。

    为什么单独一路：FTS 的 trigram 片段是 **OR** 连接的（"com" OR "omm" …），
    丢掉了"词组整体出现"这一强信号——实测库里 12 个含 "Command Code" 的单元
    在查询 "command" 下**11 个连候选池都没进**（bm25 对长文本不利 + 中文向量
    模型对英文短语弱），而内容里明明写着 `Command Code（api.commandcode.ai…）`。
    单个长标识符（`wait_for_element`）已由 identifier 通道覆盖，**多词短语**是
    盲区；两者合起来才把"字面精确证据"补齐。

    只取 ASCII 词组：中文没有空格分词，其字面匹配由 trigram 覆盖；混排串
    （"github网络失败"）里的英文部分交给 identifier/向量通道。

    **连续英文串要拆成滑动窗口**，不能整串吃下：查询 "deep seek command code"
    若当成一个短语，库里几乎不可能连着写这五个词；拆成 2 词窗口
    （"deep seek" / "seek command" / "command code"）才能命中真正存在的词组。
    2 词窗口优先（"command code"、"api key" 是最常见形态），再补 3 词窗口
    （"wait for element" 这类）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        key = p.lower()
        if len(p) >= _PHRASE_MIN_LEN and key not in seen:
            seen.add(key)
            out.append(p)

    for m in _PHRASE_RE.finditer(query):
        tokens = m.group(0).split()
        for size in (2, 3):
            for i in range(len(tokens) - size + 1):
                add(" ".join(tokens[i:i + size]))
                if len(out) >= _PHRASE_CAP:
                    return out
    return out


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


def subset_vector_search(conn: sqlite3.Connection, vec_table: str,
                         qvec: np.ndarray, k: int,
                         allow_ids: set[int]) -> ChannelHits:
    """**子集内的**向量检索：只在 allow_ids（如页面层 213 条）里取 top-k。

    为什么需要独立通道：页面是整页长文本，单一向量对长文本的相似度天然低于
    短条目——混在 2.3 万条的大池子里做 top-30，页面几乎排不进去（实测：
    200 名内只有 2 个页面）。给异质候选源各自一个通道，是本引擎既有的设计
    （向量/全文/标识符三路本就是异质信号），页面层同理。

    vec0 的 KNN 查询不支持业务过滤 → 超采样后在 Python 侧按 allow_ids 筛。
    页面量级小（数百），超采样成本可接受。
    """
    if not allow_ids:
        return []
    fetch = min(len(allow_ids) * 3, 2000)
    rows = conn.execute(
        f"SELECT rowid, distance FROM {vec_table}"
        " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (qvec.astype(np.float32).tobytes(), fetch)).fetchall()
    out: ChannelHits = []
    for rid, dist in rows:
        if rid in allow_ids:
            out.append((rid, max(1.0 - float(dist), 0.0)))
            if len(out) >= k:
                break
    return out


def subset_fts_search(conn: sqlite3.Connection, query: str, k: int,
                      allow_ids: set[int]) -> ChannelHits:
    """**子集内的**全文检索（页面层第二回路）。

    向量对长页面区分度弱（相关 0.826 与不相干 0.686~0.785 分数重叠，实测），
    而全文对**精确术语**很敏感——"容错语义""traceId"这类词命中就是命中。
    两条回路一起投票（RRF），比单靠向量更可靠。
    """
    if not allow_ids or not query.strip():
        return []
    q = query.strip()
    rows = []
    if len(q) >= TRIGRAM_MIN_LEN:
        chunks = _query_chunks(q)
        if chunks:
            match = " OR ".join(f'"{_fts_escape(c)}"' for c in chunks)
            rows = conn.execute(
                "SELECT rowid FROM units_fts WHERE units_fts MATCH ?"
                " ORDER BY bm25(units_fts, 1.0, 2.0) LIMIT ?",
                (match, max(k * 20, 200))).fetchall()
    if not rows:
        like = f"%{_like_esc(q)}%"
        rows = conn.execute(
            "SELECT id FROM units u WHERE u.text LIKE ? ESCAPE '\\' LIMIT ?",
            (like, max(k * 20, 200))).fetchall()
    out: ChannelHits = []
    for i, (rid,) in enumerate(rows):
        if rid in allow_ids:
            out.append((rid, 1.0 / (i + 1)))
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


def phrase_search(conn: sqlite3.Connection, phrases: list[str], k: int,
                  *, exclude_ids: set[int] | None = None) -> ChannelHits:
    """**短语整体匹配**：查询中的多词短语必须在文本/标题里**连续出现**才算命中。

    与 ``identifier_search`` 同源——都是"字面精确证据"，返回恒强分 1.0：命中的
    单元几乎必然真的在讲那件事（`Command Code（api.commandcode.ai…）`）。

    为什么用 LIKE 而不是 FTS：LIKE 是"整串连续出现"的判据，**不受 bm25 长文本
    劣势影响**（记忆条目长 → bm25 低 → 被 FTS 候选截断挤出，正是实测里 11/12
    个单元消失的原因），也**不受 FTS 候选 LIMIT 截断影响**。
    """
    if not phrases:
        return []
    excl = exclude_ids or set()
    counts: dict[int, int] = {}
    for ph in phrases:
        like = f"%{_like_esc(ph)}%"
        for (uid,) in conn.execute(
            "SELECT id FROM units"
            " WHERE text LIKE ? ESCAPE '\\' OR IFNULL(title,'') LIKE ? ESCAPE '\\'",
            (like, like),
        ).fetchall():
            if uid not in excl:
                counts[uid] = counts.get(uid, 0) + 1
    ordered = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:k]
    return [(uid, 1.0) for uid, _ in ordered]


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
    base = ("id, source, conversation_id, seq, role, turn_key, time, title, text")
    # src_id / wiki_path 由 ensure_bridge_schema 幂等补列（页面层召回需要）；
    # 老库或极简测试库可能缺列 → 降级为不含它们的查询（kind 退化为 message）
    try:
        rows = conn.execute(
            f"SELECT {base}, src_id, wiki_path, origin FROM units"
            f" WHERE id IN ({marks})", ids)
    except sqlite3.OperationalError:
        try:
            rows = conn.execute(
                f"SELECT {base}, src_id, wiki_path FROM units"
                f" WHERE id IN ({marks})", ids)
        except sqlite3.OperationalError:
            rows = conn.execute(
                f"SELECT {base} FROM units WHERE id IN ({marks})", ids)
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
    candidate_k: int | None = None,   # None = 用档位（rag.retrieval.recall_level）
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
    threshold_floor: float | None = None,   # None = 用档位
    origin: str = "",          # ""=全部 | "native"=自有沉淀 | "external"=外部投喂
    log: logging.Logger | None = None,
) -> list[Hit]:
    log = log or logging.getLogger("asrag.search")
    spec = settings.active_spec
    t0 = time.perf_counter()
    # **召回严格度档位**（统一配置）：候选宽度与相对阈值都由档位给，
    # 调用方显式传参则优先（测试与特殊场景需要精确控制）。
    profile = settings.recall_profile
    if candidate_k is None:
        candidate_k = int(profile.get("candidate_k",
                                      DEFAULT_RETRIEVAL["candidate_k"]))
    if threshold_floor is None:
        threshold_floor = float(profile.get("threshold_floor",
                                            THRESHOLD_FLOOR))
    conn = open_index(settings.index_db)
    conn.row_factory = sqlite3.Row
    embedder = embedder or get_embedder(spec, settings=settings)
    try:
        ensure_search_schema(conn, log=log)
        excl = _excl_ids(conn, exclude_session)
        idents = extract_identifiers(query) if mode == "hybrid" else []
        phrases = extract_phrases(query) if mode in ("hybrid", "fts") else []
        # 页面池（L2 知识页）：通道隔离与末端准入都要用，这里查一次
        page_policy = settings.page_policy
        page_ids = {r[0] for r in conn.execute(
            "SELECT id FROM units WHERE source='wiki'")}

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
        # 短语整体匹配（多词英文词组如 "command code"）：与 ident 同为"字面精确
        # 证据"。补的是中英混合专有名词的盲区——见 phrase_search 的实测说明。
        if phrases:
            ph = phrase_search(conn, phrases, candidate_k, exclude_ids=excl)
            if ph:
                channels["phrase"] = ph

        # 页面层独立通道（L2 知识页）：异质候选源各自成路——页面是长文本
        # 聚合产物，混在大池子里几乎排不进 top-k（实测 200 名内仅 2 页）。
        # **两个回路**：向量（语义）+ 全文（精确术语）——向量对长页面区分度弱
        # （相关 0.826 与不相干 0.686~0.785 重叠），全文能精确锁定含词页面，
        # 两路一起投票更可靠。页面命中后与向量/全文/标识符一起参与 RRF 融合。
        if mode in ("hybrid", "vector"):
            page_k = int(page_policy.get("channel_k", PAGE_CHANNEL_K))
            if page_ids:
                try:
                    qv_page = embedder.encode_query(query)
                    channels["page"] = subset_vector_search(
                        conn, spec.vec_table, qv_page, page_k, page_ids)
                except Exception:           # 页面通道失败不影响主召回
                    pass
                try:
                    pf = subset_fts_search(conn, query, page_k, page_ids)
                    if pf:
                        channels["page_fts"] = pf
                except Exception:
                    pass

        cand = fuse_channels(channels)
        rel = {uid: relevance_of(e) for uid, e in cand.items()}

        # 来源过滤（可选）：只保留指定来源的候选——自有记忆沉淀 vs 外部投喂
        # 是两类可信度语境不同的数据，调用方可按需只看一类。
        if origin and rel:
            ometa = _fetch_units(conn, list(rel))
            for uid, m in ometa.items():
                o = (m["origin"] if "origin" in m.keys() else None) or "native"
                if o != origin:
                    rel.pop(uid, None)

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
            # 页面不参与通用阈值（rel 天然低一个量级，会系统性误杀；页面有
            # 自己的准入策略：配额 + 证据排序，见 apply_page_policy）
            rel, bypassed = threshold_filter(rel, cand, floor=threshold_floor,
                                             exempt_ids=page_ids)
        else:
            bypassed = set()

        ranked = rank_by_relevance(rel)
        # 页面层准入（在多样性之前）：配额 + 门限 + 强信号一致性。见
        # apply_page_policy 的实测依据——页面刷屏来自"从未判定该不该进结果"。
        ranked, dropped_pages = apply_page_policy(
            ranked, cand, page_ids, policy=page_policy)
        if diversity:
            pool = ranked[: max(k * 4, k)]
            metas = _fetch_units(conn, [i for i, _ in pool])
            # 会话限席只约束会话轨迹；原子记忆（source='memory'）与知识页
            # （source='wiki'）各自独立成席——否则同域的多张页面会互相挤占
            conv_of = {i: (m["source"],
                           f"u{i}" if m["source"] in ("memory", "wiki")
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

        # 准入页面保底占位：相关页融合分可能只有 0.21×top，纯按分数会被挤出 k
        top = reserve_pages(top, ranked, page_ids, policy=page_policy)

        by_id = _fetch_units(conn, [i for i, _ in top])
        first_rank = {ch: {uid: i for i, (uid, _) in enumerate(hits, 1)}
                      for ch, hits in channels.items()}
        hits: list[Hit] = []
        for unit_id, score in top:
            r = by_id.get(unit_id)
            if r is None:
                continue
            entry = cand.get(unit_id, {})
            src_id = r["src_id"] if "src_id" in r.keys() else None
            kind = kind_of(src_id)      # 注意：不要用 k（那是 top-k 参数）
            origin = ((r["origin"] if "origin" in r.keys() else None)
                      or "native")
            text = r["text"]
            summary = None
            wiki_path = None
            if kind == "page":
                # 两阶段：正文截断（整页中位 1639 / 最长 7221 字符），
                # 摘要单独给出，需要细节时按路径读全文
                m = re.search(r"摘要：(.+)", text)
                summary = m.group(1).strip() if m else None
                if "wiki_path" in r.keys():
                    wiki_path = r["wiki_path"]
                if len(text) > PAGE_TEXT_CAP:
                    text = text[:PAGE_TEXT_CAP].rstrip() + "…"
            hit = Hit(
                unit_id=unit_id, source=r["source"],
                conversation_id=r["conversation_id"], seq=r["seq"],
                role=r["role"], turn_key=r["turn_key"], time=r["time"],
                title=r["title"], text=text, score=score,
                vec_rank=entry.get("vec", (None,))[0],
                fts_rank=entry.get("fts", (None,))[0],
                ident_rank=entry.get("ident", (None,))[0],
                bypassed=unit_id in bypassed,
                kind=kind, wiki_path=wiki_path, summary=summary,
                origin=origin, signals=frozenset(entry),
            )
            if expand_turns and kind != "page":
                # 页面层没有"轮次上下文"可展开（它是聚合产物）
                hit.turn_context = expand_turn(
                    conn, hit.source, hit.conversation_id, hit.turn_key)
            hits.append(hit)

        if judge is not None:
            before = len(hits)
            hits = judge.filter(query, hits)
            log.info("judge applied in=%d out=%d", before, len(hits))

        log.info(
            "search q=%r mode=%s k=%d idents=%s phrases=%s vec_n=%d fts_n=%d"
            " ident_n=%d phrase_n=%d dropped_low=%d bypassed=%d page_dropped=%d"
            " excl=%d fused_ids=%s cost_ms=%d",
            query, mode, k, idents, phrases, len(channels.get("vec", [])),
            len(channels.get("fts", [])), len(channels.get("ident", [])),
            len(channels.get("phrase", [])),
            len(dropped_low), len(bypassed), len(dropped_pages), len(excl),
            [i for i, _ in top], int((time.perf_counter() - t0) * 1000))
        return hits
    finally:
        conn.close()
