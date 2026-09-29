# -*- coding: utf-8 -*-
"""全局通知：给「需要人看见的事」一个落点。

## 与日志的分工

`logs/` 是**事实流水**（发生过什么，append-only，写完不再变）；通知是**待办队列**
（有已读/未读、有生命周期、要被查询与处理）。两者都要有：一次批量提交失败，既要
进 `logs/wiki.log` 供事后排查，也要进通知让**人**知道。

## 为什么是 SQLite 而不是队列

单机应用，没有多消费者、没有削峰需求 —— 队列是过度设计。一张表足够：按等级 /
类别 / 未读查询、标记已读、按天清理。**解耦**体现在调用方只
`from agentmemhub.notify import notify`，不关心存储、降级与落盘细节。

## 绝不阻塞主流程

通知是旁路：落库失败**降级**写 `logs/notify_fallback.jsonl`，函数**永不抛**。
一次 wiki 更新不该因为"通知写不进去"而失败。

## 分级

`level` ∈ `debug` / `info` / `warning` / `error`（沿用需求里的写法；写进
`logs/notify.log` 时由 `logs.audit_notify` 映射成日志流的 `warn`）；
`category` 常见值 `batch` / `wiki` / `trigger` / `config`。两者都不做白名单拒绝
—— 只做取值校验与回落（非法 level 回落 `info`）。**通知丢一条比分类不标准更糟**。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

#: 通知等级（用户语义；日志流用 `warn`，映射在 `logs.audit_notify`）
LEVELS = ("debug", "info", "warning", "error")

#: 常见来源类别（不做白名单限制，供调用方参考与筛选）
CATEGORIES = ("batch", "wiki", "trigger", "config")

#: 清理时默认保留天数（与日志段的 `keep_days` 同思路）
DEFAULT_KEEP_DAYS = 30

DB_NAME = "notify.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL    NOT NULL,
  level    TEXT    NOT NULL,
  category TEXT    NOT NULL,
  title    TEXT    NOT NULL,
  body     TEXT    NOT NULL DEFAULT '',
  meta     TEXT    NOT NULL DEFAULT '{}',
  read_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_notify_ts ON notifications (ts DESC);
"""


def db_path() -> Path:
    """通知库位置：`<data_dir>/notify.db`。

    放 data_dir 是有意的：它随数据目录一起被快照/备份覆盖，且**与源库、索引库
    同处一地**，运维时不必记第二个位置。
    """
    from agentmemhub import config as hub_config
    return Path(hub_config.config().data_dir) / DB_NAME


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)      # 幂等：每次连接都确保表在（首次使用免初始化）
    return conn


@contextmanager
def _db(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """短连接 + 提交 + **必定关闭**。

    注意：`with sqlite3.connect(...)` 只管事务、**不关连接**，直接那样写会漏 fd。
    """
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fallback(row: dict, err: Exception) -> None:
    """落库失败时的兜底：追加 JSONL；**它再失败也只能咽下去**。"""
    try:
        from agentmemhub.logs import log_dir
        p = Path(log_dir()) / "notify_fallback.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**row, "fallback_error": str(err)[:200]},
                               ensure_ascii=False) + "\n")
    except Exception:                    # noqa: BLE001
        pass


def _audit(row: dict, ok: bool) -> None:
    try:
        from agentmemhub.logs import audit_notify
        audit_notify({"event": "notify_write", "ok": ok, "level": row["level"],
                      "category": row["category"], "title": row["title"]})
    except Exception:                    # noqa: BLE001
        pass


def notify(level: str, category: str, title: str, body: str = "", *,
           meta: Optional[dict[str, Any]] = None, db: Optional[str] = None,
           ts: Optional[float] = None) -> bool:
    """写一条通知，返回是否**落库成功**（失败已降级，绝不抛）。"""
    lv = str(level or "info").strip().lower()
    if lv not in LEVELS:
        lv = "info"
    row = {"ts": float(ts if ts is not None else time.time()),
           "level": lv,
           "category": str(category or "general").strip().lower(),
           "title": str(title or "")[:200],
           "body": str(body or "")[:4000],
           "meta": dict(meta or {})}
    ok = False
    try:
        with _db(db) as conn:
            conn.execute(
                "INSERT INTO notifications (ts, level, category, title, body, meta)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (row["ts"], row["level"], row["category"], row["title"],
                 row["body"], json.dumps(row["meta"], ensure_ascii=False)))
        ok = True
    except Exception as e:               # noqa: BLE001 —— 旁路，永不抛
        _fallback(row, e)
    _audit(row, ok)
    return ok


def list_notifications(*, level: str = "", category: str = "",
                       unread_only: bool = False, limit: int = 50,
                       db: Optional[str] = None) -> list[dict[str, Any]]:
    """查通知（最新在前）。库/表还不存在时返回空表 —— 与 `load_state` 同哲学。"""
    sql = "SELECT * FROM notifications"
    where: list[str] = []
    args: list[Any] = []
    if level:
        where.append("level = ?")
        args.append(str(level).lower())
    if category:
        where.append("category = ?")
        args.append(str(category).lower())
    if unread_only:
        where.append("read_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(int(limit))
    try:
        with _db(db) as conn:
            rows = conn.execute(sql, args).fetchall()
    except Exception:                    # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:                # noqa: BLE001
            d["meta"] = {}
        d["unread"] = d.get("read_at") is None
        out.append(d)
    return out


def count_unread(*, db: Optional[str] = None) -> int:
    """未读条数（面板/CLI 的角标用）。库不可用时返回 0 而不是抛。"""
    try:
        with _db(db) as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"
            ).fetchone()[0])
    except Exception:                    # noqa: BLE001
        return 0


def mark_read(ids: Optional[list[int]] = None, *, all: bool = False,
              db: Optional[str] = None) -> int:
    """标记已读，返回受影响行数。`all=True` 时忽略 `ids`。"""
    now = time.time()
    try:
        with _db(db) as conn:
            if all:
                cur = conn.execute(
                    "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
                    (now,))
            elif ids:
                marks = ",".join("?" for _ in ids)
                cur = conn.execute(
                    "UPDATE notifications SET read_at = ? WHERE read_at IS NULL"
                    " AND id IN (%s)" % marks,
                    (now, *[int(i) for i in ids]))
            else:
                return 0
            return int(cur.rowcount or 0)
    except Exception:                    # noqa: BLE001
        return 0


def prune(keep_days: int = DEFAULT_KEEP_DAYS, *, db: Optional[str] = None) -> int:
    """清掉超过保留期的通知，返回删除条数。`keep_days <= 0` = 不限（不动手）。"""
    days = int(keep_days or 0)
    if days <= 0:
        return 0
    cut = time.time() - days * 86400
    try:
        with _db(db) as conn:
            cur = conn.execute("DELETE FROM notifications WHERE ts < ?", (cut,))
            return int(cur.rowcount or 0)
    except Exception:                    # noqa: BLE001
        return 0
