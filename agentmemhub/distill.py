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

import sqlite3

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
