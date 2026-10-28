"""源库 schema 漂移时的容错（2026-10-26）。

背景：`SqliteConversationAdapter.load()` 过去按列名硬取 `s["project_id"]` /
`s["slug"]` / `s["permission"]` 等（`WorkBuddyAdapter` 同型，取 10 个列名）。
这些列是源工具**后来才加**的，版本一换就可能消失：

1. `load()` 只有 `try/finally` 没有 `except` ⇒ `IndexError` 穿到 `cli.run_ingest`
   ⇒ **整个 sync 中断**（后面的源本轮全不采）；
2. `adapters/__init__.py::load_all()` 的 `except Exception` 又把它吞成
   `result[source] = []` ⇒ 用户只看到"这个源今天没有新会话"，查不出原因。

所以判定标准是：**少几列也要能把会话读出来**，只有主键缺失才跳过。
"""
import sqlite3
from pathlib import Path

from agentmemhub.adapters.opencode import OpenCodeAdapter


def _mk_db(path: Path, *, session_cols: str, session_values: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE session ({session_cols})")
    conn.execute(f"INSERT INTO session VALUES ({session_values})")
    # message / part 保持最小可用结构（`_load_events` 会按 time_created 排序）
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, "
                 "time_created REAL)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, data TEXT, "
                 "time_created REAL)")
    conn.commit()
    conn.close()


def test_会话表缺少可选列仍能读出会话(tmp_path):
    """只有 id/title 也能读 —— 缺 project_id/slug/permission/directory 不该炸。"""
    db = tmp_path / "opencode.db"
    _mk_db(db, session_cols="id TEXT, title TEXT",
           session_values="'s1', '一个会话'")
    out = OpenCodeAdapter().load(db)
    assert len(out) == 1
    assert out[0]["id"] == "s1"
    assert out[0]["title"] == "一个会话"
    assert out[0]["cwd"] == ""              # 缺 directory ⇒ 空串而不是异常
    assert out[0]["meta"] == {}             # 缺的可选 meta 全部省略


def test_会话表多出未知列也不影响(tmp_path):
    """源库加了新列（SELECT *）不该影响读取。"""
    db = tmp_path / "opencode.db"
    _mk_db(db, session_cols="id TEXT, title TEXT, brand_new_col TEXT, directory TEXT",
           session_values="'s2', '新版本', 'x', '/work'")
    out = OpenCodeAdapter().load(db)
    assert len(out) == 1
    assert out[0]["cwd"] == "/work"


def test_缺少主键列时跳过而不是抛(tmp_path):
    """没有 id 列无法归属会话 —— 跳过并返回空列表，仍不中断整轮采集。"""
    db = tmp_path / "opencode.db"
    _mk_db(db, session_cols="title TEXT", session_values="'没有主键'")
    assert OpenCodeAdapter().load(db) == []


def test_清单缺time_updated列时回落到仅id(tmp_path):
    """`list_sessions` 只取 id/time_updated：缺时间列时 updated_at 记 0。"""
    db = tmp_path / "opencode.db"
    _mk_db(db, session_cols="id TEXT, title TEXT", session_values="'s3', 'x'")
    rows = OpenCodeAdapter().list_sessions(db)
    assert rows == [{"id": "s3", "updated_at": 0}]
