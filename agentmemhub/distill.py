"""记忆蒸馏：原始会话 → 结构化、可沉淀的记忆条目。

为什么需要（实测）：索引库 14635 条 unit 中近 30% 是 <50 字符的对话填充词
（「继续」「可以」「ok」）；单轮 13 条 unit 里 10 条是过程叙述。逐条向量化
原始消息 → 召回面噪音极高。蒸馏把「一轮对话」提炼为「几条结论」。

流水线（详见 docs/memory-distillation.md）：
    S0 切片（纯本地）→ S1 段级蒸馏（LLM①）→ S2 同会话合并沉淀（LLM②）
    → S3 跨会话去重（向量近邻三档标记）→ S4 入库投影

存储分工：
- `distill_hashes`：幂等层。内容 hash + prompt_ver 相同 → 重跑跳过
  （用户要求：同一会话重复执行不得产生重复记忆）；
- `distilled_memories`：蒸馏真相源（type/topic/confidence/去重链完整保留）；
- `units`（source='distilled'）：检索投影 —— 走既有三路召回 / MCP / 面板，
  召回侧零改动。

表名用 `distilled_memories` 而非 `memories`：与 `memstore.py` 的手动原子记忆
（`units.source='memory'`，来自 MCP memory_save）在语义上明确区隔。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

# 话题边界检测复用 FTS 侧的分词口径（3-gram 步长 1 + 中文噪音表），
# 保证"切片看到的话题"与"检索命中时看到的话题"是同一套词法。
from agentmemhub.rag.search import TRIGRAM_MIN_LEN, _CJK_NOISE

#: 记忆类型枚举（LLM 输出受限，避免自由发挥导致分类不可用）
MEMORY_TYPES = ("decision", "fact", "preference", "lesson")
#: 置信度枚举（枚举比数字刻度更稳定：LLM 对枚举的遵循率明显更高）
CONFIDENCES = ("high", "medium", "low")
#: 入库状态：new=新增；similar=与既有条目相似（入库但打标互链）；duplicate=重复（丢弃）
MEMORY_STATUSES = ("new", "similar", "duplicate")

#: units 投影用的 source 值（与 MCP 原子记忆的 'memory' 区隔）
DISTILLED_SOURCE = "distilled"
#: units 投影的 src_id 前缀（幂等锚；对齐 memstore 的 'mcp_' 前缀思想）
DISTILLED_SRC_PREFIX = "dst_"
#: 单片会话（未切片）的 slice_key
SLICE_WHOLE = "whole"

_SCHEMA_DISTILL = """
CREATE TABLE IF NOT EXISTS distill_hashes(
    source          TEXT    NOT NULL,
    conversation_id TEXT    NOT NULL,
    slice_key       TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    prompt_ver      INTEGER NOT NULL,
    model           TEXT,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY(source, conversation_id, slice_key, prompt_ver)
);
CREATE INDEX IF NOT EXISTS idx_distill_hashes_conv
    ON distill_hashes(source, conversation_id);

CREATE TABLE IF NOT EXISTS distilled_memories(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL,
    conversation_id  TEXT    NOT NULL,
    slice_key        TEXT,
    turn_key         TEXT,
    type             TEXT    NOT NULL,
    topic            TEXT,
    content          TEXT    NOT NULL,
    confidence       TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'new',
    dedup_of         INTEGER,
    content_hash     TEXT    NOT NULL,
    prompt_ver       INTEGER NOT NULL,
    model            TEXT,
    merged_from_json TEXT,
    created_at       INTEGER NOT NULL,
    UNIQUE(source, conversation_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_dm_conv   ON distilled_memories(source, conversation_id);
CREATE INDEX IF NOT EXISTS idx_dm_status ON distilled_memories(status);
CREATE INDEX IF NOT EXISTS idx_dm_hash   ON distilled_memories(content_hash);
"""


def ensure_distill_schema(conn: sqlite3.Connection) -> None:
    """幂等建表（重复调用安全；与 open_index 的 schema 并行不冲突）。"""
    conn.executescript(_SCHEMA_DISTILL)
    conn.commit()


def check_memory_fields(mem_type: str, confidence: str) -> str | None:
    """校验枚举字段；返回拒绝原因（None=通过）。

    蒸馏是 fail-open 设计：非法条目丢弃并记录原因，不中断整批。
    """
    if mem_type not in MEMORY_TYPES:
        return f"type 非法：{mem_type!r}（允许 {MEMORY_TYPES}）"
    if confidence not in CONFIDENCES:
        return f"confidence 非法：{confidence!r}（允许 {CONFIDENCES}）"
    return None


# ══════════════════════════════════════════════════════════════════════
# S0 切片器（纯本地，零 LLM 成本）
#
# 分流规则（实测依据：239 会话中 >24k 字符的仅 26 个却占 80% 语料）：
#   ① 整会话 ≤ 双预算 → 单片（slice_key='whole'）
#   ② 超预算 → 按轮累积，轮数或字符任一触顶即切；切点用关键词重叠度
#      在预算内前移至话题转换处（避免把同一话题从中间切开）
#   ③ 单轮自身超预算（导入型巨会话，实测最大 262 万字符仅 1 轮）→
#      轮内按字符硬切，切点对齐行边界
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Turn:
    """一轮对话：同一 turn_key 下的消息序列（按源 seq 顺序）。"""

    turn_key: str
    messages: tuple[tuple[str, str], ...]   # (role, content)
    chars: int


@dataclass(frozen=True)
class Slice:
    """一个待蒸馏片段。"""

    source: str
    conversation_id: str
    slice_key: str                 # SLICE_WHOLE | "s0" | "s1" …
    turn_first: str | None         # 首轮 turn_key（单片会话为 None）
    turn_last: str | None          # 末轮 turn_key（单片会话为 None）
    chars: int                     # 渲染后正文长度
    content_hash: str              # 幂等键（内容指纹）
    text: str                      # 送 LLM 的正文
    turns: tuple[str, ...]         # 覆盖的 turn_key（S2 分组与溯源）


def turns_from_events(rows) -> list[Turn]:
    """把 (seq, turn_key, role, content) 行（须按 seq 升序）分组为轮。

    同一 turn_key 的消息归为一轮（即使中间穿插其它事件也合并，保证"一轮
    就是一轮"）；极少数无 turn_key 的事件（实测 0.1%）按自身 seq 成轮，
    不丢内容。
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for seq, turn_key, role, content in rows:
        key = turn_key or f"seq:{seq}"
        groups.setdefault(key, []).append((role, content or ""))
    return [
        Turn(turn_key=k, messages=tuple(v),
             chars=sum(len(c) for _, c in v))
        for k, v in groups.items()
    ]


def _tokens(text: str) -> set[str]:
    """话题词法集合：3-gram 滑窗（步长 1），全噪音块剔除。

    与 FTS 侧同一口径（TRIGRAM_MIN_LEN / _CJK_NOISE），保证"切片看到的
    话题"与"检索命中时看到的话题"一致。不做数量截断（相似度比较需要全量）。
    """
    out: set[str] = set()
    if len(text) < TRIGRAM_MIN_LEN:
        return out
    for i in range(len(text) - TRIGRAM_MIN_LEN + 1):
        chunk = text[i:i + TRIGRAM_MIN_LEN]
        if all(ch in _CJK_NOISE for ch in chunk):
            continue
        out.add(chunk)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    """集合相似度；两侧皆空视为 1.0（无信息 → 不构成话题切换证据）。"""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _refine_boundary(turns: list[Turn], lo: int, hi: int,
                     cache: dict[int, set[str]]) -> int:
    """在 [lo, hi] 内选"相邻两轮话题差异最大"处作切点（hi 为预算硬上限）。

    相似度并列时取更靠后的（片更大 → 切片数更少）。
    """
    best_j, best_sim = hi, None
    for j in range(lo, hi + 1):
        if j <= 0 or j >= len(turns):
            continue
        if j not in cache:
            cache[j] = _tokens(_turn_text(turns[j]))
        if j - 1 not in cache:
            cache[j - 1] = _tokens(_turn_text(turns[j - 1]))
        sim = _jaccard(cache[j - 1], cache[j])
        if best_sim is None or sim <= best_sim:
            best_sim, best_j = sim, j
    return best_j


def _turn_text(turn: Turn) -> str:
    return "\n".join(c for _, c in turn.messages)


def _split_long_text(text: str, limit: int) -> list[str]:
    """超长文本按行边界切成 ≤limit 的块（单行本身超长则硬切）。"""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def _hard_split_turn(turn: Turn, limit: int) -> list[list[tuple[str, str]]]:
    """轮内硬切：把单轮拆为多个消息组（每片字符 ≤limit）。

    用于导入型巨会话（单轮即超预算）。此处**不施加 per_message_cap**——
    巨轮内容本就该被完整切片，截断会丢掉 99% 的信息。
    """
    parts: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    cur_len = 0
    for role, content in turn.messages:
        for piece in _split_long_text(content, limit):
            if cur and cur_len + len(piece) > limit:
                parts.append(cur)
                cur, cur_len = [], 0
            cur.append((role, piece))
            cur_len += len(piece)
    if cur:
        parts.append(cur)
    return parts


def _render(groups: list[tuple[str, tuple[tuple[str, str], ...]]],
            per_message_cap: int | None) -> str:
    """渲染送 LLM 的正文：轮次与角色标注；可选单条截断。"""
    lines: list[str] = []
    for i, (turn_key, msgs) in enumerate(groups, 1):
        tag = f"…{turn_key[-8:]}" if len(turn_key) > 8 else turn_key
        lines.append(f"[轮次 {i} | {tag}]")
        for role, content in msgs:
            c = (content or "").strip()
            if per_message_cap and len(c) > per_message_cap:
                c = c[:per_message_cap] + "…"
            lines.append(f"[{role}] {c}")
    return "\n".join(lines)


def fingerprint(text: str | None) -> str:
    """内容指纹（幂等键）：规范化去空行后 sha256 前 16 位。"""
    norm = "\n".join(
        line.strip() for line in (text or "").splitlines() if line.strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _choose_bounds(turns: list[Turn], *, max_chars: int, max_turns: int,
                   topic_boundary: bool, window: int,
                   cache: dict[int, set[str]]) -> list[int]:
    """返回各片结束索引（不含）；预算按**原文**字符数控制（保守）。"""
    bounds: list[int] = []
    start, n = 0, len(turns)
    while start < n:
        acc, hi = 0, start
        while hi < n:
            if acc + turns[hi].chars > max_chars or (hi - start) + 1 > max_turns:
                break
            acc += turns[hi].chars
            hi += 1
        if hi == start:            # 首轮即超预算（巨轮）→ 该轮独占一片
            hi = start + 1
        if topic_boundary and hi < n and hi - start > 1:
            lo = max(start + 1, hi - window)
            hi = _refine_boundary(turns, lo, hi, cache)
        bounds.append(hi)
        start = hi
    return bounds


def _make_slice(source: str, cid: str, slice_key: str,
                groups: list[tuple[str, tuple[tuple[str, str], ...]]],
                *, per_message_cap: int | None,
                whole: bool) -> Slice:
    text = _render(groups, per_message_cap)
    keys = tuple(k for k, _ in groups)
    return Slice(
        source=source, conversation_id=cid, slice_key=slice_key,
        turn_first=None if whole else keys[0],
        turn_last=None if whole else keys[-1],
        chars=len(text), content_hash=fingerprint(text), text=text,
        turns=keys,
    )


def build_slices(source: str, conversation_id: str, turns: list[Turn], *,
                 max_chars: int = 24000, max_turns: int = 16,
                 topic_boundary: bool = True, boundary_window: int = 4,
                 per_message_cap: int = 2000) -> list[Slice]:
    """把会话的轮序列切分为待蒸馏片段（纯函数，无 IO）。"""
    if not turns:
        return []
    total = sum(t.chars for t in turns)
    if total <= max_chars and len(turns) <= max_turns:
        groups = [(t.turn_key, t.messages) for t in turns]
        return [_make_slice(source, conversation_id, SLICE_WHOLE, groups,
                            per_message_cap=per_message_cap, whole=True)]

    cache: dict[int, set[str]] = {}
    bounds = _choose_bounds(turns, max_chars=max_chars, max_turns=max_turns,
                            topic_boundary=topic_boundary,
                            window=boundary_window, cache=cache)
    slices: list[Slice] = []
    idx = 0
    for end in bounds:
        group = turns[idx:end]
        idx = end
        # 单轮独占且超预算 → 轮内硬切（巨会话）；此处不施加单条截断
        if len(group) == 1 and group[0].chars > max_chars:
            for part in _hard_split_turn(group[0], max_chars):
                slices.append(_make_slice(
                    source, conversation_id, f"s{len(slices)}",
                    [(group[0].turn_key, tuple(part))],
                    per_message_cap=None, whole=False))
            continue
        slices.append(_make_slice(
            source, conversation_id, f"s{len(slices)}",
            [(t.turn_key, t.messages) for t in group],
            per_message_cap=per_message_cap, whole=False))
    return slices


# ── 采集库读取（源库只读；排除过滤与摄取侧同语义）──────────────────────


def _has_exclusions(src: sqlite3.Connection) -> bool:
    """源库是否具备排除表（采集库恒有；外部/测试源库可能没有）。"""
    return src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
        " AND name='memory_exclusions'").fetchone() is not None


def _exclusion_clause() -> str:
    """排除过滤：整会话（turn_key=''）或该轮 → 整条不参与蒸馏。

    与 rag/ingest.py 的 _iter_candidates 同语义，保证"不写入记忆的会话/轮次
    也不会被蒸馏"（父级覆盖子级的层级语义由 store.add_exclusion 维护）。
    """
    return """
              AND NOT EXISTS (
                    SELECT 1 FROM memory_exclusions x
                    WHERE x.source = e.source
                      AND x.conversation_id = e.conversation_id
                      AND (x.turn_key = '' OR x.turn_key = IFNULL(e.turn_key,''))
              )"""


def conversation_turns(src: sqlite3.Connection, source: str, conversation_id: str,
                       *, roles: tuple[str, ...] = ("user", "assistant")) -> list[Turn]:
    """读某会话的轮序列（按 seq 升序）。

    过滤：角色白名单、空/空白内容、系统注入事件、记忆排除（会话级或轮级）。
    """
    placeholders = ",".join("?" * len(roles))
    excl = _exclusion_clause() if _has_exclusions(src) else ""
    rows = src.execute(
        f"""
        SELECT e.seq, e.turn_key, e.role, e.content
        FROM events e
        WHERE e.source = ? AND e.conversation_id = ?
          AND e.role IN ({placeholders})
          AND e.content IS NOT NULL AND TRIM(e.content) <> ''
          AND IFNULL(e.is_system, 0) = 0
          {excl}
        ORDER BY e.seq
        """,
        [source, conversation_id, *roles]).fetchall()
    return turns_from_events([(r[0], r[1], r[2], r[3]) for r in rows])
