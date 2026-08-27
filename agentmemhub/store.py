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
    """默认数据库位置（可用 AGENTMEMHUB_DB 环境变量覆盖）。"""
    env = os.environ.get("AGENTMEMHUB_DB")
    if env:
        return Path(env)
    data_dir = Path(os.environ.get(
        "AGENTMEM_HUB_DATA_DIR",
        Path.home() / ".agentmemhub",
    ))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "agentmemhub.db"


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
                    event_total += 1

        return event_total

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
            cur = conn.execute(
                "DELETE FROM events WHERE source=? AND conversation_id=?",
                (source, conversation_id),
            )
            n_events = cur.rowcount
            conn.execute(
                "DELETE FROM events_fts WHERE source=? AND conversation_id=?",
                (source, conversation_id),
            )
            cur2 = conn.execute(
                "DELETE FROM conversations WHERE source=? AND id=?",
                (source, conversation_id),
            )
            if cur2.rowcount == 0:
                raise KeyError(f"conversation not found: {source}/{conversation_id}")
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
