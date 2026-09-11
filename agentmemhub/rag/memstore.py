"""引擎写侧（R2）：原子记忆落库 + 价值评分存储。

分工铁律：引擎「存值不判值」——本模块只做确定性算术（feedback 聚合、
幂等锚、级联存储），谁来决定 value（LLM 评分）永远在 AgentMemHub 策略层。

MemOS 语义兼容点（迁移/回退依赖）：
- 原子记忆：source='memory'、conversation_id='mcp'（对应 MemOS episodeId/sessionId="mcp"）；
- 幂等锚：src_id = "mcp_" + sha256(content)[:16]，与 mcp_server._save 现行算法逐字一致；
- feedback 聚合：value = r_human = clamp(Σsigned/Σmag, ±1)，priority = max(old, |value|)
  （MemOS memory-core.ts:2465-2478 同源规则）。
"""
from __future__ import annotations

import hashlib

import numpy as np
import sqlite3
import time
from typing import Sequence

from .config import Settings
from .embedder import Embedder
from .ingest import ensure_vec_table, open_index
from .runtime import get_active_embedder

MEMSTORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS unit_values(
    unit_id INTEGER PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0,
    r_human REAL,
    priority REAL NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'explicit',
    polarity TEXT NOT NULL,
    magnitude REAL NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uf_unit ON unit_feedback(unit_id);
-- src_id 业务锚索引（与 ingest.ensure_bridge_schema 幂等同源）：记忆报表
-- JOIN 与投影点查依赖；缺失时带筛选的 JOIN 会全表扫 units（实测 20s/次）
CREATE INDEX IF NOT EXISTS idx_units_src ON units(src_id);
"""

POLARITY_SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def ensure_memstore_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MEMSTORE_SCHEMA)
    # manual_value：用户手动锁定的权重（优先于聚合 value，且不参与时间衰减）。
    # 幂等加列（vec0 不能加列，普通表可以；列存在时跳过）。
    cols = [r[1] for r in conn.execute("PRAGMA table_info(unit_values)")]
    if "manual_value" not in cols:
        conn.execute("ALTER TABLE unit_values ADD COLUMN manual_value REAL")
    conn.commit()


def content_anchor(content: str) -> str:
    """与 mcp_server._save 的 id 算法一致：mcp_ + sha256[:16]（迁移防重锚）。"""
    return "mcp_" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


#: 来源初始置信度（规则：Agent 主动写入 > 离线批量蒸馏）。
#: 只是**起点**——之后由用户反馈与手动加权自然演化，不是终身标签。
BASE_VALUE_AGENT_WRITE = 0.6    # Agent 经 MCP memory_save 主动写入（有明确意图）
BASE_VALUE_DISTILLED = 0.3      # 离线批量蒸馏（默认中等，靠使用升降）


def ensure_base_value(conn: sqlite3.Connection, unit_id: int, base: float) -> None:
    """给新记忆写来源初始价值分（已有任何记录则不覆盖，保留演化结果）。"""
    ensure_memstore_schema(conn)
    row = conn.execute(
        "SELECT unit_id FROM unit_values WHERE unit_id=?", (unit_id,)).fetchone()
    if row:
        return
    with conn:
        conn.execute(
            "INSERT INTO unit_values(unit_id, value, r_human, priority, updated_at)"
            " VALUES(?,?,NULL,?,?)",
            (unit_id, base, base, int(time.time())))


def save_memory(
    settings: Settings,
    content: str,
    *,
    embedder: Embedder | None = None,
    ts: int | None = None,
    title: str | None = None,
) -> dict:
    """原子记忆写入：units + 即时嵌入 + FTS 触发器。幂等：同内容重放返回既有单元。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("空内容不可入库")
    src_id = content_anchor(text)
    conn = open_index(settings.index_db)
    spec = settings.active_spec
    try:
        ensure_memstore_schema(conn)
        ensure_vec_table(conn, spec)
        hit = conn.execute(
            "SELECT id FROM units WHERE source='memory' AND src_id=?",
            (src_id,)).fetchone()
        if hit:
            return {"unit_id": hit[0], "created": False, "src_id": src_id}
        emb = embedder or get_active_embedder(settings)
        vec = emb.encode_passages([text])[0]
        ts = int(ts if ts is not None else time.time())
        with conn:  # 单事务：units+vec 原子落库
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM units"
                " WHERE source='memory' AND conversation_id='mcp'").fetchone()[0]
            cur = conn.execute(
                "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
                " src_id, time, title, text, chars) VALUES('memory','mcp',?,?,'mcp',?,?,?,?,?)",
                (seq, "user", src_id, ts, title or "记忆", text, len(text)))
            conn.execute(
                f"INSERT INTO {spec.vec_table}(rowid, embedding) VALUES(?,?)",
                (cur.lastrowid, np.ascontiguousarray(vec, dtype=np.float32).tobytes()))
        ensure_base_value(conn, cur.lastrowid, BASE_VALUE_AGENT_WRITE)
        return {"unit_id": cur.lastrowid, "created": True, "src_id": src_id}
    finally:
        conn.close()


def set_manual_value(
    conn: sqlite3.Connection,
    unit_id: int,
    value: float | None,
    *,
    ts: int | None = None,
) -> dict:
    """用户手动加权：直接锁定该记忆的价值分（优先于自动聚合，且不随时间衰减）。

    value=None 表示清除手动设定，回到自动聚合值。越界值钳制到 [0,1]。
    """
    ensure_memstore_schema(conn)
    if not conn.execute("SELECT 1 FROM units WHERE id=?", (unit_id,)).fetchone():
        raise KeyError(f"unit 不存在: {unit_id}")
    now = int(ts if ts is not None else time.time())
    with conn:
        row = conn.execute(
            "SELECT value, r_human, priority FROM unit_values WHERE unit_id=?",
            (unit_id,)).fetchone()
        if row is None:
            # 首次建值：base 按该 unit 的来源取初始置信度
            # （蒸馏=0.3 / Agent 主动写入=0.6），清除手动后能回到正确起点
            role = conn.execute("SELECT role FROM units WHERE id=?",
                                (unit_id,)).fetchone()
            base = (BASE_VALUE_DISTILLED if role and role[0] == "distilled"
                    else BASE_VALUE_AGENT_WRITE)
            base = value if value is not None else base
            conn.execute(
                "INSERT INTO unit_values(unit_id, value, r_human, priority,"
                " manual_value, updated_at) VALUES(?,?,NULL,?,?,?)",
                (unit_id, base, base, value, now))
        elif value is None:
            conn.execute(
                "UPDATE unit_values SET manual_value=NULL, updated_at=?"
                " WHERE unit_id=?", (now, unit_id))
        else:
            v = min(max(float(value), 0.0), 1.0)
            conn.execute(
                "UPDATE unit_values SET manual_value=?, priority=MAX(priority,?),"
                " updated_at=? WHERE unit_id=?", (v, v, now, unit_id))
    final = conn.execute(
        "SELECT COALESCE(manual_value, value) FROM unit_values WHERE unit_id=?",
        (unit_id,)).fetchone()[0]
    return {"unit_id": unit_id, "manual_value": value, "effective": final}


def set_user_feedback(
    conn: sqlite3.Connection,
    unit_id: int,
    polarity: str | None,
    *,
    magnitude: float = 1.0,
    channel: str = "explicit",
    ts: int | None = None,
) -> dict:
    """面板用户反馈（**状态式、可撤销**）：一个 unit 在同一 channel 只保留一条。

    polarity=None → 取消（删除既有反馈后重算）。

    与 put_feedback 的语义差异（两者并存、各司其职）：
    - put_feedback：**历史累加**（MemOS 同源：多条证据累积取有符号均值），
      MCP memory_score / 批量评分沿用——同一条多个 harness 反复表态是证据；
    - set_user_feedback：**当前表态**（面板 👍/👎）。反复点不应叠加稀释
      （均值会越点越钝），取消即干净回退——按钮态与库内状态一一对应。

    value 重算：仍有反馈 → 有符号均值；无任何反馈 → 回来源初始分
    （蒸馏 0.3 / Agent 主动写入 0.6）且 r_human 清空。
    manual_value（用户 ⭐ 锁定）永不由此函数改动。
    """
    if polarity is not None and polarity not in POLARITY_SIGN:
        raise ValueError(f"非法 polarity: {polarity!r}")
    ensure_memstore_schema(conn)
    row = conn.execute("SELECT role FROM units WHERE id=?", (unit_id,)).fetchone()
    if row is None:
        raise KeyError(f"unit 不存在: {unit_id}")
    role = row[0]
    mag = min(max(float(magnitude), 0.0), 1.0)
    now = int(ts if ts is not None else time.time())
    with conn:
        conn.execute("DELETE FROM unit_feedback WHERE unit_id=? AND channel=?",
                     (unit_id, channel))
        if polarity is not None:
            conn.execute(
                "INSERT INTO unit_feedback(unit_id, channel, polarity, magnitude,"
                " created_at) VALUES(?,?,?,?,?)",
                (unit_id, channel, polarity, mag, now))
        agg = conn.execute(
            "SELECT SUM(magnitude * CASE polarity"
            "        WHEN 'positive' THEN 1.0 WHEN 'negative' THEN -1.0 ELSE 0.0 END),"
            "       SUM(magnitude), COUNT(*) FROM unit_feedback WHERE unit_id=?",
            (unit_id,)).fetchone()
        signed, total, n = agg[0] or 0.0, agg[1] or 0.0, agg[2] or 0
        old = conn.execute("SELECT priority FROM unit_values WHERE unit_id=?",
                           (unit_id,)).fetchone()
        old_prio = old[0] if old else 0.0
        if n:
            value = max(min(signed / total, 1.0), -1.0)
            r_human = value
            priority = max(old_prio or 0.0, abs(value))
        else:
            # 无反馈 → 回来源初始分（与 ensure_base_value 同口径）
            base = (BASE_VALUE_DISTILLED if role == "distilled"
                    else BASE_VALUE_AGENT_WRITE)
            value, r_human, priority = base, None, max(old_prio or 0.0, base)
        conn.execute(
            "INSERT INTO unit_values(unit_id, value, r_human, priority, updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(unit_id) DO UPDATE SET value=excluded.value,"
            " r_human=excluded.r_human, priority=excluded.priority,"
            " updated_at=excluded.updated_at",     # 注意：不动 manual_value
            (unit_id, value, r_human, priority, now))
    return {"unit_id": unit_id, "value": value, "r_human": r_human,
            "priority": priority, "polarity": polarity,
            "feedback_count": n, "revoked": polarity is None}


def put_feedback(
    conn: sqlite3.Connection,
    unit_id: int,
    polarity: str,
    *,
    magnitude: float = 1.0,
    channel: str = "explicit",
    ts: int | None = None,
) -> dict:
    """写反馈并即时聚合（MemOS 同源规则）。unit 不存在 → KeyError（404 语义）。"""
    if polarity not in POLARITY_SIGN:
        raise ValueError(f"非法 polarity: {polarity!r}")
    ensure_memstore_schema(conn)
    if not conn.execute("SELECT 1 FROM units WHERE id=?", (unit_id,)).fetchone():
        raise KeyError(f"unit 不存在: {unit_id}")
    mag = min(max(float(magnitude), 0.0), 1.0)
    now = int(ts if ts is not None else time.time())
    with conn:
        conn.execute(
            "INSERT INTO unit_feedback(unit_id, channel, polarity, magnitude,"
            " created_at) VALUES(?,?,?,?,?)",
            (unit_id, channel, polarity, mag, now))
        agg = conn.execute(
            "SELECT SUM(magnitude * CASE polarity"
            "        WHEN 'positive' THEN 1.0 WHEN 'negative' THEN -1.0 ELSE 0.0 END),"
            "       SUM(magnitude) FROM unit_feedback WHERE unit_id=?",
            (unit_id,)).fetchone()
        signed, total = agg[0] or 0.0, agg[1] or 0.0
        value = max(min(signed / total, 1.0), -1.0) if total else 0.0
        old = conn.execute(
            "SELECT priority FROM unit_values WHERE unit_id=?", (unit_id,)).fetchone()
        priority = max(old[0] if old else 0.0, abs(value))
        conn.execute(
            "INSERT INTO unit_values(unit_id, value, r_human, priority, updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(unit_id) DO UPDATE SET value=excluded.value,"
            " r_human=excluded.r_human, priority=excluded.priority,"
            " updated_at=excluded.updated_at",
            (unit_id, value, value, priority, now))
    return {"unit_id": unit_id, "value": value, "r_human": value,
            "priority": priority}


class ValueStore:
    """读侧 ValueProvider 实现（ext.ValueProvider 协议）：按 unit_id 现查库。

    供 hybrid_search(value_provider=...) 注入；每次调用独立开库连接，
    跨线程/进程安全（配合 busy_timeout 的 WAL 库）。
    """

    def __init__(self, index_db, *, use_priority: bool = False):
        self.index_db = str(index_db)
        self.use_priority = use_priority

    def values(self, unit_ids: Sequence[int]) -> dict[int, float]:
        ids = list(unit_ids)
        if not ids:
            return {}
        conn = sqlite3.connect(f"file:{self.index_db}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(ids))
            # 手动加权优先：用户锁定的值覆盖自动聚合值
            col = ("COALESCE(manual_value, priority)" if self.use_priority
                   else "COALESCE(manual_value, value)")
            rows = conn.execute(
                f"SELECT unit_id, {col} FROM unit_values WHERE unit_id IN ({marks})",
                ids).fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.OperationalError:
            return {}  # 值表未建（引擎全新状态）→ 中性
        finally:
            conn.close()

    def no_decay_ids(self, unit_ids: Sequence[int]) -> set[int]:
        """手动加权的 unit 集合——用户锁定的价值不随时间衰减。"""
        ids = list(unit_ids)
        if not ids:
            return set()
        conn = sqlite3.connect(f"file:{self.index_db}?mode=ro", uri=True)
        try:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT unit_id FROM unit_values WHERE unit_id IN ({marks})"
                " AND manual_value IS NOT NULL", ids).fetchall()
            return {r[0] for r in rows}
        except sqlite3.OperationalError:
            return set()
        finally:
            conn.close()


def list_units(conn: sqlite3.Connection, *, limit: int = 20,
               offset: int = 0, source: str | None = None) -> list[sqlite3.Row]:
    """recent/评分枚举统一出口：units 左连 unit_values，时间倒序。"""
    conn.row_factory = sqlite3.Row
    where = "WHERE source=?" if source else ""
    args: list = [source] if source else []
    args += [limit, offset]
    return conn.execute(
        f"""SELECT u.id, u.source, u.conversation_id, u.seq, u.role, u.turn_key,
                   u.time, u.title, substr(u.text,1,300) AS text,
                   v.value, v.priority
            FROM units u LEFT JOIN unit_values v ON v.unit_id = u.id
            {where}
            ORDER BY u.time DESC, u.id DESC LIMIT ? OFFSET ?""", args).fetchall()


def memstore_stats(conn: sqlite3.Connection) -> dict:
    ensure_memstore_schema(conn)
    return {
        "units": conn.execute("SELECT COUNT(*) FROM units").fetchone()[0],
        "memory_units": conn.execute(
            "SELECT COUNT(*) FROM units WHERE source='memory'").fetchone()[0],
        "valued": conn.execute("SELECT COUNT(*) FROM unit_values").fetchone()[0],
        "feedback": conn.execute("SELECT COUNT(*) FROM unit_feedback").fetchone()[0],
    }
