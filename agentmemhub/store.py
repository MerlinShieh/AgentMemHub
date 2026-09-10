"""AgentMemHub 核心存储层。

SQLite 存储：conversations（会话元数据）+ events（全量事件流）+ events_fts（FTS5 全文搜索）。

写入走「事务内清空重写」策略（配合 source signature 增量重建），
与 ai-conversation-hub 的 signature 机制保持一致。
"""

from __future__ import annotations

import json
import sqlite3
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from agentmemhub.models import Event


def default_db_path() -> Path:
    """SQLite 位置：统一配置（agentmemhub.yaml / 环境变量）→ 默认 <项目根>/database。"""
    from agentmemhub import config
    cfg = config.config()
    return cfg.db_path


class Store:
    """会话与事件存储。"""

    def __init__(self, db_path: Optional[Path] = None, *,
                 check_same_thread: bool = True):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._check_same_thread = check_same_thread
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # 连接与 schema
    # ------------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=self._check_same_thread)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL：断电最多丢最后一个事务，不损库（写入吞吐显著提升）
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        self.conn.executescript(schema_path.read_text(encoding="utf-8"))
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """老库升级：CREATE TABLE IF NOT EXISTS 不会给已有表补列，探测式 ALTER（幂等）。"""
        conn = self.conn
        ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        for col, ddl in (
            ("src_id", "ALTER TABLE events ADD COLUMN src_id TEXT"),
            ("turn_key", "ALTER TABLE events ADD COLUMN turn_key TEXT"),
            ("is_system", "ALTER TABLE events ADD COLUMN is_system INTEGER DEFAULT 0"),
        ):
            if col not in ev_cols:
                conn.execute(ddl)
        cv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
        if "session_key" not in cv_cols:
            conn.execute("ALTER TABLE conversations ADD COLUMN session_key TEXT")

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def replace_source(
        self,
        source: str,
        sessions: list[dict[str, Any]],
        *,
        signature: str = "",
    ) -> int:
        """原子替换某个 source 的全部会话+事件（增量重建单位 = source）。

        session: {source, id, title, cwd, created_at, updated_at, model,
                  meta, events: [Event]}
        返回写入的事件总数。
        """
        conn = self.conn
        with conn:
            # 清空旧数据
            conn.execute("DELETE FROM events_fts WHERE source = ?", (source,))
            conn.execute("DELETE FROM events WHERE source = ?", (source,))
            conn.execute("DELETE FROM conversations WHERE source = ?", (source,))

            event_total = 0
            for sess in sessions:
                event_total += self._insert_session(conn, source, sess,
                                                    signature=signature)
        return event_total

    def _insert_session(self, conn: sqlite3.Connection, source: str,
                        sess: dict[str, Any], *, signature: str = "") -> int:
        """写单个会话（conversations + events + FTS 行）。返回写入事件数。

        须在调用方事务内执行（不自行开启/提交事务）。
        """
        events = sess.get("events") or []
        cid = str(sess.get("id", ""))
        roles = [e.role for e in events]

        conn.execute(
            """INSERT OR REPLACE INTO conversations
               (source, id, title, cwd, model, created_at, updated_at,
                event_count, roles_json, meta_json, signature, session_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source, cid, sess.get("title", ""), sess.get("cwd", ""),
                sess.get("model", ""),
                sess.get("created_at") or 0,
                sess.get("updated_at") or 0,
                len(events),
                json.dumps(roles, ensure_ascii=False),
                json.dumps(sess.get("meta") or {}, ensure_ascii=False),
                signature,
                sess.get("session_key") or None,
            ),
        )

        n = 0
        for e in events:
            tok_input = None
            if e.tool_input is not None:
                tok_input = json.dumps(e.tool_input, ensure_ascii=False)
            conn.execute(
                """INSERT OR REPLACE INTO events
                   (source, conversation_id, seq, role, content,
                    tool_name, tool_input_json, tool_output, tool_status,
                    reasoning, patch_file, patch_diff, shell_cmd,
                    shell_output, shell_cwd, parent_id, time, model, raw_json,
                    src_id, turn_key, is_system)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source, cid, e.seq, e.role, e.content,
                    e.tool_name, tok_input, e.tool_output, e.tool_status,
                    e.reasoning, e.patch_file, e.patch_diff, e.shell_cmd,
                    e.shell_output, e.shell_cwd, e.parent_id,
                    int(e.time) if e.time else None, e.model, e.raw_json,
                    e.src_id, e.turn_key,
                    (1 if e.is_system else 0) if e.is_system is not None else 0,
                ),
            )
            # FTS 行（role UNINDEXED，正文可搜）
            fts_doc = (e.role, e.content or "", e.tool_name or "",
                       e.tool_output or "", e.reasoning or "",
                       e.shell_cmd or "", e.shell_output or "",
                       e.patch_diff or "")
            conn.execute(
                "INSERT INTO events_fts (source, conversation_id, role, content, tool_name, tool_output, reasoning, shell_cmd, shell_output, patch_diff) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source, cid) + fts_doc,
            )
            n += 1
        return n

    def upsert_sessions(
        self,
        source: str,
        sessions: list[dict[str, Any]],
        *,
        signature: str = "",
        force: bool = False,
    ) -> dict[str, int]:
        """会话级增量写入：新增插入；updated_at（或事件数）变化则整会话重写；
        未变化跳过；force=True 忽略对比全部重写（源级新鲜度信号触发整源重扫）。

        与 replace_source 的差异：不删除「源端已消失」的会话（历史保全），
        单事务，增量粒度 = 会话。返回 {"added", "updated", "unchanged", "events"}。
        """
        conn = self.conn
        added = updated = unchanged = 0
        events_total = 0
        with conn:
            for sess in sessions:
                cid = str(sess.get("id", ""))
                events = sess.get("events") or []
                new_updated = sess.get("updated_at") or 0
                row = conn.execute(
                    "SELECT updated_at, event_count FROM conversations "
                    "WHERE source=? AND id=?",
                    (source, cid),
                ).fetchone()
                if row is not None:
                    stored_updated = row["updated_at"] or 0
                    if (not force
                            and abs(new_updated - stored_updated) < 1e-6
                            and len(events) == (row["event_count"] or 0)):
                        unchanged += 1
                        continue
                    self._delete_conversation_rows(conn, source, cid)
                    updated += 1
                else:
                    added += 1
                events_total += self._insert_session(conn, source, sess,
                                                     signature=signature)
        return {"added": added, "updated": updated,
                "unchanged": unchanged, "events": events_total}

    def delete_source(self, source: str) -> None:
        conn = self.conn
        with conn:
            conn.execute("DELETE FROM events_fts WHERE source = ?", (source,))
            conn.execute("DELETE FROM events WHERE source = ?", (source,))
            conn.execute("DELETE FROM conversations WHERE source = ?", (source,))

    def delete_conversation(self, source: str, conversation_id: str) -> int:
        """删除单个会话（事务级联 conversations/events/FTS）。返回删除的事件数。"""
        conn = self.conn
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE source=? AND id=?",
                (source, conversation_id),
            ).fetchone()
            if exists is None:
                raise KeyError(f"conversation not found: {source}/{conversation_id}")
            n_events = self._delete_conversation_rows(conn, source, conversation_id)
        return n_events

    @staticmethod
    def _delete_conversation_rows(conn: sqlite3.Connection, source: str,
                                  cid: str) -> int:
        """删除单会话三表行（事务内 helper，不自行开事务）。返回删除事件数。"""
        cur = conn.execute(
            "DELETE FROM events WHERE source=? AND conversation_id=?",
            (source, cid),
        )
        n_events = cur.rowcount
        conn.execute(
            "DELETE FROM events_fts WHERE source=? AND conversation_id=?",
            (source, cid),
        )
        conn.execute(
            "DELETE FROM conversations WHERE source=? AND id=?",
            (source, cid),
        )
        return n_events

    def update_title(self, source: str, conversation_id: str, title: str) -> bool:
        """更新会话标题。返回该会话是否存在。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE conversations SET title=? WHERE source=? AND id=?",
                (title.strip(), source, conversation_id),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 记忆排除（R6）：控制哪些会话/轮次不写入记忆索引
    # 意图真相源——独立表，不随 replace_source/整源重建被清空
    # ------------------------------------------------------------------

    def add_exclusion(self, source: str, conversation_id: str,
                      turn_key: str = "", note: str = "") -> bool:
        """落排除标记。turn_key='' 表示整会话。返回是否新建（False=已存在）。"""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO memory_exclusions"
                "(source, conversation_id, turn_key, created_at, note)"
                " VALUES(?,?,?,?,?)",
                (source, conversation_id, turn_key or "", int(time.time()),
                 note or None))
        return cur.rowcount > 0

    def remove_exclusion(self, source: str, conversation_id: str,
                         turn_key: str = "") -> bool:
        """取消排除标记。返回是否有行被删。"""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM memory_exclusions WHERE source=? AND conversation_id=?"
                " AND turn_key=?", (source, conversation_id, turn_key or ""))
        return cur.rowcount > 0

    def list_exclusions(self, source: Optional[str] = None,
                        conversation_id: Optional[str] = None
                        ) -> list[sqlite3.Row]:
        """排除清单（不传参 = 全库；供面板渲染勾选态与统计）。"""
        q = ("SELECT source, conversation_id, turn_key, created_at, note"
             " FROM memory_exclusions")
        where, params = [], []
        if source:
            where.append("source=?")
            params.append(source)
        if conversation_id:
            where.append("conversation_id=?")
            params.append(conversation_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY source, conversation_id, turn_key"
        return self.conn.execute(q, params).fetchall()

    def exclusion_state(self, source: str, conversation_id: str
                        ) -> tuple[bool, set[str]]:
        """返回 (整会话是否排除, 被排除的 turn_key 集合)。"""
        rows = self.list_exclusions(source, conversation_id)
        whole = any(not r["turn_key"] for r in rows)
        turns = {r["turn_key"] for r in rows if r["turn_key"]}
        return whole, turns

    def peers_with_turn(self, source: str, conversation_id: str,
                        turn_key: str) -> list[tuple[str, str]]:
        """找出同源同会话之外、含该 turn_key 的其他 (source, conversation_id) 副本。

        现实场景：同一段对话可能被多个 Agent 各自采集（如 opencode 原生
        与 zcode 的导入副本），turn_key 相同但 source 不同。轮次排除若只作用
        于当前会话，副本仍会被召回——面板据此提示「同步排除 N 个副本」。
        """
        if not turn_key:
            return []
        return [
            (r["source"], r["conversation_id"])
            for r in self.conn.execute(
                "SELECT DISTINCT source, conversation_id FROM events"
                " WHERE turn_key=? AND NOT (source=? AND conversation_id=?)",
                (turn_key, source, conversation_id))
        ]

    # ------------------------------------------------------------------
    # 记忆清洗：系统注入事件（is_system）统计/删除
    # ------------------------------------------------------------------

    def system_event_counts(self, source: Optional[str] = None, *,
                            conversations: Optional[list[tuple[str, str]]] = None
                            ) -> list[sqlite3.Row]:
        """统计系统注入事件数（clean --dry-run 预览用）。

        conversations：可选 (source, conversation_id) 列表——只统计这些会话
        （delta 增量清洗）；None = 全库。
        """
        q = ("SELECT source, COUNT(*) AS n, COUNT(DISTINCT conversation_id) AS convs "
             "FROM events WHERE is_system = 1")
        params: list = []
        if source:
            q += " AND source = ?"
            params.append(source)
        conn = self.conn
        with conn:
            if conversations is not None:
                self._prepare_delta_targets(conn, conversations)
                q += (" AND EXISTS (SELECT 1 FROM _delta_targets t WHERE "
                      "t.source = events.source AND t.cid = events.conversation_id)")
            q += " GROUP BY source ORDER BY n DESC"
            rows = conn.execute(q, params).fetchall()
            if conversations is not None:
                conn.execute("DROP TABLE IF EXISTS temp._delta_targets")
        return rows

    @staticmethod
    def _prepare_delta_targets(conn: sqlite3.Connection,
                               conversations: list[tuple[str, str]]) -> None:
        """把 (source, conversation_id) 目标集装进临时表（无 SQL 参数上限）。"""
        conn.execute("DROP TABLE IF EXISTS temp._delta_targets")
        conn.execute("CREATE TEMP TABLE _delta_targets (source TEXT, cid TEXT)")
        conn.executemany("INSERT INTO _delta_targets (source, cid) VALUES (?,?)",
                         [(s, c) for s, c in conversations])

    def delete_system_events(self, source: Optional[str] = None, *,
                             conversations: Optional[list[tuple[str, str]]] = None
                             ) -> tuple[int, int]:
        """删除系统注入事件，并重建受影响会话的 FTS 索引与 event_count。

        conversations：可选 (source, conversation_id) 列表——只处理这些会话
        （delta 增量清洗）；None = 全库。破坏性操作：调用方应先预览。
        返回 (删除事件数, 受影响会话数)。
        """
        conn = self.conn
        src_q = "" if source is None else " AND source = ?"
        src_p: list = [] if source is None else [source]
        tgt_q = (" AND EXISTS (SELECT 1 FROM _delta_targets t WHERE "
                 "t.source = events.source AND t.cid = events.conversation_id)")
        with conn:
            if conversations is not None:
                self._prepare_delta_targets(conn, conversations)
            else:
                tgt_q = ""
            affected = conn.execute(
                "SELECT DISTINCT source, conversation_id FROM events "
                "WHERE is_system = 1" + src_q + tgt_q, src_p).fetchall()
            cur = conn.execute("DELETE FROM events WHERE is_system = 1" + src_q + tgt_q,
                               src_p)
            deleted = cur.rowcount
            if conversations is not None:
                conn.execute("DROP TABLE IF EXISTS temp._delta_targets")
            for (s, cid) in affected:
                # 重建该会话 FTS（events_fts 无 seq 列，删除行无法精确对应 → 整体重建）
                conn.execute(
                    "DELETE FROM events_fts WHERE source=? AND conversation_id=?",
                    (s, cid))
                evs = conn.execute(
                    "SELECT role, content, tool_name, tool_output, reasoning, "
                    "shell_cmd, shell_output, patch_diff FROM events "
                    "WHERE source=? AND conversation_id=? ORDER BY seq",
                    (s, cid)).fetchall()
                for e in evs:
                    conn.execute(
                        "INSERT INTO events_fts (source, conversation_id, role, content, "
                        "tool_name, tool_output, reasoning, shell_cmd, shell_output, "
                        "patch_diff) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (s, cid, e["role"], e["content"] or "", e["tool_name"] or "",
                         e["tool_output"] or "", e["reasoning"] or "",
                         e["shell_cmd"] or "", e["shell_output"] or "",
                         e["patch_diff"] or ""))
                conn.execute(
                    "UPDATE conversations SET event_count=(SELECT COUNT(*) FROM events "
                    "WHERE source=? AND conversation_id=?) WHERE source=? AND id=?",
                    (s, cid, s, cid))
        return deleted, len(affected)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_conversations(self, source: Optional[str] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM conversations"
        params: tuple = ()
        if source:
            q += " WHERE source = ?"
            params = (source,)
        q += " ORDER BY updated_at DESC"
        return self.conn.execute(q, params).fetchall()

    def get_conversation(self, source: str, cid: str) -> Optional[sqlite3.Row]:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE source = ? AND id = ?",
            (source, cid),
        ).fetchone()
        return row

    def get_events(self, source: str, cid: str) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE source = ? AND conversation_id = ? ORDER BY seq",
            (source, cid),
        ).fetchall()
        events = []
        for r in rows:
            ev = Event(
                role=r["role"], seq=r["seq"], time=r["time"],
                content=r["content"], parent_id=r["parent_id"],
                tool_name=r["tool_name"],
                tool_input=json.loads(r["tool_input_json"]) if r["tool_input_json"] else None,
                tool_output=r["tool_output"], tool_status=r["tool_status"],
                reasoning=r["reasoning"],
                patch_file=r["patch_file"], patch_diff=r["patch_diff"],
                shell_cmd=r["shell_cmd"], shell_output=r["shell_output"],
                shell_cwd=r["shell_cwd"], model=r["model"], raw_json=r["raw_json"],
                src_id=r["src_id"], turn_key=r["turn_key"],
                is_system=bool(r["is_system"]) if r["is_system"] else None,
            )
            events.append(ev)
        return events

    def stats(self) -> dict[str, Any]:
        conn = self.conn
        conv_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        ev_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        sources = [
            dict(r) for r in conn.execute(
                "SELECT source, COUNT(*) AS conversations FROM conversations GROUP BY source"
            ).fetchall()
        ]
        return {
            "conversations": conv_count,
            "events": ev_count,
            "sources": sources,
        }

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        role: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """搜索事件正文。

        策略：
        - 查询含 CJK（连续中文）时用 LIKE 子串匹配 —— unicode61/trigram
          tokenizer 对 2 字符中文短词（如"登录"）无法命中，LIKE 最可靠。
        - 无 CJK（英文/词组）时用 FTS5 MATCH + snippet 高亮片段。
        """
        conn = self.conn
        if _has_cjk(query):
            return self._search_like(query, source=source, role=role, limit=limit)
        return self._search_fts(query, source=source, role=role, limit=limit)

    def _search_fts(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        role: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conn = self.conn
        match = _fts_query(query)
        sql = (
            "SELECT f.source, f.conversation_id, f.role, f.content, f.tool_name, "
            "snippet(events_fts, 3, '', '', '…', 12) AS snippet, "
            "c.title, c.cwd, c.updated_at "
            "FROM events_fts f JOIN conversations c "
            "ON c.source = f.source AND c.id = f.conversation_id "
            "WHERE events_fts MATCH ?"
        )
        params: list[Any] = [match]
        if source:
            sql += " AND f.source = ?"
            params.append(source)
        if role:
            sql += " AND f.role = ?"
            params.append(role)
        sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _search_like(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        role: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """LIKE 子串搜索（中文可靠），JOIN 会话元数据。"""
        conn = self.conn
        like = f"%{query}%"
        sql = (
            "SELECT e.source, e.conversation_id, e.role, e.content, e.tool_name, '' AS snippet, "
            "c.title, c.cwd, c.updated_at "
            "FROM events e JOIN conversations c "
            "ON c.source = e.source AND c.id = e.conversation_id "
            "WHERE (e.content LIKE ?1 OR e.tool_output LIKE ?1 OR e.reasoning LIKE ?1 "
            " OR e.shell_cmd LIKE ?1 OR e.shell_output LIKE ?1 OR e.patch_diff LIKE ?1)"
        )
        params: list[Any] = [like]
        if source:
            sql += " AND e.source = ?"
            params.append(source)
        if role:
            sql += " AND e.role = ?"
            params.append(role)
        sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _fts_query(query: str) -> str:
    """普通文本 → FTS5 MATCH 表达式（短语包裹，防 SQL 注入破坏语法）。"""
    q = query.strip()
    if not q:
        return '""'
    # 已带引号或运算符则原样
    if any(op in q for op in ('"', "AND", "OR", "NOT")):
        return q
    # 含空格视为短语
    if " " in q:
        return f'"{q}"'
    return f'"{q}"'


def _has_cjk(text: str) -> bool:
    """是否含 CJK（中日韩）字符 —— 有则走 LIKE 子串搜索。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def source_signature(path: Path) -> str:
    """源文件指纹：mtime + 大小（增量重建依据）。"""
    if not path.exists():
        return ""
    try:
        if path.is_file():
            st = path.stat()
            return hashlib.sha256(
                f"{path}:{st.st_mtime_ns}:{st.st_size}".encode()
            ).hexdigest()[:16]
        # 目录：汇总所有文件
        h = hashlib.sha256()
        for p in sorted(path.rglob("*")):
            if p.is_file():
                try:
                    st = p.stat()
                    h.update(f"{p}:{st.st_mtime_ns}:{st.st_size}|".encode())
                except OSError:
                    pass
        return h.hexdigest()[:16]
    except OSError:
        return ""
