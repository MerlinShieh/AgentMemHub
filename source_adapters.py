from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from readable import readable_turn_text

_SKILL_ROOT = Path(__file__).resolve().parent / "skills" / "find-agent-data"
if _SKILL_ROOT.is_dir() and str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))


EXTRA_SOURCES = (
    "claude",
    "cursor",
    "qclaw",
    "qoderwork",
    "zcode",
    "codepilot",
    "marvis",
    "qoder",
    "qodercn",
    "qwenworkcn",
    "grok",
)
MAX_CLAUDE_TRANSCRIPT_BYTES = 100 * 1024 * 1024
CUSTOM_SOURCE_PREFIX = "custom_"
CUSTOM_FORMATS = {"jsonl", "markdown", "sqlite"}
SOURCE_LABELS = {
    "claude": "Claude Code",
    "cursor": "Cursor",
    "qclaw": "QClaw",
    "qoderwork": "QoderWork",
    "zcode": "ZCode",
    "codepilot": "CodePilot",
    "marvis": "Marvis",
    "qoder": "Qoder",
    "qodercn": "QoderCN",
    "qwenworkcn": "千问办公",
    "grok": "Grok Build",
}
# QoderWork 产品改名谱系（Qoder -> QoderWork -> 千问办公/QwenWork），
# 同一台机器可能同时存在新旧两代数据目录，需要合并读取以免丢对话
QODERWORK_FAMILY_DIRS = ("QoderWork CN", "QoderWork", "QwenWorkCN", "QwenWork")
SKIP_DISCOVERY_DIRS = {
    ".git", ".svn", "__pycache__", "node_modules", ".venv", "venv", "cache",
    "caches", "backup", "backups", "temp", "tmp", "$recycle.bin",
}
SECRET_PATTERNS = (
    (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer [REDACTED]"),
    (r"\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{12,}", "[REDACTED_TOKEN]"),
    (
        r"(?i)\b(api[_ -]?key|access[_ -]?token|secret|password)\b"
        r"(\s*[:=]\s*)[^\s,;\"']{6,}",
        r"\1\2[REDACTED]",
    ),
)
MARKDOWN_ROLE_MARKER = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(user|assistant|human|ai|用户|助手)\s*[:：]?\s*$"
)


@contextmanager
def readonly_db(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def sqlite_tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        with readonly_db(path) as conn:
            return {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except (OSError, sqlite3.DatabaseError):
        return set()


def redact(value: Any, limit: int = 20000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    for pattern, replacement in SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text[:limit].rstrip()


def epoch(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        while number > 10_000_000_000:
            number /= 1000
        return number
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        try:
            return epoch(float(text))
        except ValueError:
            return 0.0


def basename(value: Any) -> str:
    text = str(value or "").rstrip("\\/")
    return text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "无工作区"


def content_text(value: Any, role: str = "assistant") -> str:
    return redact(readable_turn_text(role, value))


def json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def conversation(
    source: str,
    conversation_id: Any,
    title: Any,
    messages: list[dict[str, Any]],
    *,
    cwd: Any = "",
    created_at: Any = 0,
    updated_at: Any = 0,
    model: Any = "",
    archived: bool = False,
    status: str = "active",
    source_kind: str = "",
    rollout_path: Any = "",
) -> dict[str, Any]:
    user_messages = [item for item in messages if item["role"] == "user" and item["text"]]
    assistant_messages = [
        item for item in messages if item["role"] == "assistant" and item["text"]
    ]
    first_user = user_messages[0]["text"] if user_messages else ""
    safe_title = (
        redact(title, 240)
        or redact(first_user, 120)
        or f"{SOURCE_LABELS.get(source, '自定义 Agent')} 对话"
    )
    created = epoch(created_at) or min(
        (float(item["timestamp"]) for item in messages if item["timestamp"]),
        default=0,
    )
    updated = epoch(updated_at) or max(
        (float(item["timestamp"]) for item in messages if item["timestamp"]),
        default=created,
    )
    return {
        "source": source,
        "id": str(conversation_id),
        "title": safe_title,
        "preview": redact(first_user, 900) or safe_title,
        "cwd": str(cwd or ""),
        "workspace": basename(cwd),
        "created_at": created or updated,
        "updated_at": updated or created,
        "message_count": len(user_messages) + len(assistant_messages),
        "tool_call_count": 0,
        "model": redact(model, 120),
        "archived": bool(archived),
        "status": status,
        "source_kind": source_kind,
        "rollout_path": str(rollout_path or ""),
        "parent_id": "",
    }


def default_candidates(source: str) -> list[Path]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    application_support = (
        home / "Library" / "Application Support"
        if sys.platform == "darwin"
        else appdata
    )
    if source == "claude":
        configured = str(os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
        homes = [Path(configured).expanduser()] if configured else []
        homes.append(home / ".claude")
        return homes
    if source == "cursor":
        return [application_support / "Cursor" / "User" / "globalStorage"]
    if source == "qclaw":
        return [home / ".qclaw"]
    if source == "qoderwork":
        return [
            application_support / name / "data" / "agents.db"
            for name in QODERWORK_FAMILY_DIRS
        ]
    if source == "zcode":
        return [home / ".zcode" / "cli" / "db" / "db.sqlite"]
    if source == "codepilot":
        return [
            home / ".codepilot" / "codepilot.db",
            home / ".codepilot" / "chat.db",
            home / ".codepilot" / "data.db",
            application_support / "CodePilot" / "chat.db",
        ]
    if source == "marvis":
        candidates = [
            home / ".marvis" / "state.db",
            application_support / "Marvis" / "state.db",
        ]
        tencent_user = application_support / "Tencent" / "Marvis" / "User"
        if tencent_user.is_dir():
            # 多用户目录：真实用户库通常更大，排前面；default_user 空库垫底
            candidates.extend(sorted(
                tencent_user.glob("*/database/data.db"),
                key=lambda item: item.stat().st_size,
                reverse=True,
            ))
        return candidates
    if source in {"qoder", "qodercn"}:
        from agent_recovery.qoder import layout
        return list(layout(source).index_dbs)
    if source == "qwenworkcn":
        return [home / ".qwenworkcn"]
    if source == "grok":
        return [Path(os.environ.get("GROK_HOME") or (home / ".grok"))]
    return []


def _claude_transcript_files(path: Path) -> list[Path]:
    result: list[Path] = []
    for root_name in ("projects", "sessions"):
        root = path / root_name
        if not root.is_dir():
            continue
        for file_path in root.rglob("*.jsonl"):
            try:
                relative_parts = [part.casefold() for part in file_path.relative_to(root).parts]
            except ValueError:
                relative_parts = []
            if (
                "subagents" in relative_parts
                or file_path.name.casefold() in {"history.jsonl"}
                or file_path.name.casefold().startswith("agent-")
                or file_path.name.casefold().endswith(".trajectory.jsonl")
            ):
                continue
            result.append(file_path)
    return result


def _claude_history_ids(path: Path) -> set[str]:
    history = path / "history.jsonl"
    result: set[str] = set()
    if not history.is_file():
        return result
    try:
        with history.open("r", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                session_id = str(event.get("sessionId") or event.get("session_id") or "")
                if session_id and redact(event.get("display")):
                    result.add(session_id)
    except OSError:
        pass
    return result


def _claude_session_index_entries(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    roots = [path / "projects", path / "sessions"]
    index_paths: list[Path] = []
    for root in roots:
        if root.is_dir():
            index_paths.extend(root.rglob("sessions-index.json"))
    for index_path in index_paths:
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        original_path = str(payload.get("originalPath") or "")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            session_id = str(entry.get("sessionId") or entry.get("session_id") or "")
            if not session_id:
                continue
            first_prompt = _claude_user_text(entry.get("firstPrompt"))
            current = result.get(session_id)
            candidate = {
                "title": redact(
                    entry.get("customTitle")
                    or entry.get("summary")
                    or first_prompt,
                    240,
                ),
                "first_prompt": first_prompt,
                "cwd": str(entry.get("projectPath") or original_path),
                "created": epoch(entry.get("created") or entry.get("createdAt")),
                "updated": epoch(
                    entry.get("modified")
                    or entry.get("updatedAt")
                    or entry.get("fileMtime")
                ),
                "model": redact(entry.get("model"), 120),
                "index_path": index_path,
                "message_count": int(entry.get("messageCount") or 0),
            }
            if not current or candidate["updated"] >= current["updated"]:
                result[session_id] = candidate
    return result


def validate_source(source: str, path: Path) -> tuple[bool, str]:
    try:
        if source == "claude":
            if not path.is_dir():
                return False, "目录不存在"
            transcripts = _claude_transcript_files(path)
            history_ids = _claude_history_ids(path)
            index_ids = set(_claude_session_index_entries(path))
            oversized = sum(
                1
                for file_path in transcripts
                if file_path.stat().st_size > MAX_CLAUDE_TRANSCRIPT_BYTES
            )
            if not transcripts and not history_ids and not index_ids:
                return False, "未找到完整会话、history.jsonl 或 sessions-index.json"
            details = [f"{len(transcripts)} 个完整会话文件"]
            transcript_ids = {item.stem for item in transcripts}
            history_only = len(history_ids - transcript_ids)
            index_only = len(index_ids - transcript_ids - history_ids)
            if history_only:
                details.append(f"{history_only} 个历史索引会话")
            if index_only:
                details.append(f"{index_only} 个项目索引会话")
            if oversized:
                details.append(f"{oversized} 个超大文件将安全降级")
            return True, " · ".join(details)
        if source == "codepilot":
            valid = {"chat_sessions", "messages"}.issubset(sqlite_tables(path))
            return valid, "CodePilot 会话数据库" if valid else "数据库结构不匹配"
        if source == "cursor":
            valid = path.is_dir() and (path / "conversation-search.db").is_file()
            if valid:
                return True, "Cursor 本地会话搜索数据库"
            if path.is_dir() and (path / "state.vscdb").is_file():
                return False, "已发现 Cursor 状态库，但当前版本没有兼容的 conversation-search.db"
            return False, "未找到 Cursor 会话数据库"
        if source == "marvis":
            valid = {"conversations", "messages"}.issubset(sqlite_tables(path))
            return valid, "Marvis 会话数据库" if valid else "数据库结构不匹配"
        if source == "qclaw":
            valid = (
                path.is_dir()
                and (path / "agents" / "main" / "sessions" / "sessions.json").is_file()
            )
            return valid, "QClaw 主会话目录" if valid else "未找到 QClaw 主会话清单"
        if source == "qoderwork":
            valid = {"projects", "chats", "sub_chats", "messages"}.issubset(sqlite_tables(path))
            return valid, "QoderWork 会话数据库" if valid else "数据库结构不匹配"
        if source == "zcode":
            valid = {"session", "message", "part"}.issubset(sqlite_tables(path))
            return valid, "ZCode 会话数据库" if valid else "数据库结构不匹配"
        if source in {"qoder", "qodercn"}:
            if not path.is_file():
                return False, "Qoder 会话索引不存在"
            with readonly_db(path) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "chat_session" in tables:
                    count = int(conn.execute("SELECT count(*) FROM chat_session").fetchone()[0])
                    return True, f"Qoder 新版标题索引 · {count} 个会话"
                if "ItemTable" not in tables:
                    return False, "数据库结构不匹配"
                count = int(conn.execute(
                    "SELECT count(*) FROM ItemTable WHERE key LIKE 'lingma.chat.localHistory.%'"
                ).fetchone()[0])
            if count:
                return True, f"Qoder 旧版 quest 索引 · {count} 组历史"
            return False, "未找到 lingma localHistory 索引"
        if source == "qwenworkcn":
            files = _claude_transcript_files(path) if path.is_dir() else []
            if files:
                return True, f"千问办公 CLI · {len(files)} 个会话文件"
            return False, "未找到 projects 下的会话 JSONL"
        if source == "grok":
            summaries = _grok_summary_files(path) if path.is_dir() else []
            if summaries:
                return True, f"Grok Build · {len(summaries)} 个会话"
            return False, "未找到 ~/.grok/sessions 下的 summary.json"
    except (OSError, sqlite3.DatabaseError):
        return False, "读取失败"
    return False, "未知来源"


def estimate_conversations(source: str, path: Path | None) -> int:
    if not path:
        return 0
    try:
        if source == "claude":
            transcript_ids = {item.stem for item in _claude_transcript_files(path)}
            return len(
                transcript_ids
                | _claude_history_ids(path)
                | set(_claude_session_index_entries(path))
            )
        if source == "codepilot":
            with readonly_db(path) as conn:
                return int(conn.execute("SELECT count(*) FROM chat_sessions").fetchone()[0])
        if source == "cursor":
            search_db = path / "conversation-search.db"
            if not search_db.is_file():
                return 0
            with readonly_db(search_db) as conn:
                return int(
                    conn.execute(
                        "SELECT count(*) FROM conversations WHERE source='local'"
                    ).fetchone()[0]
                )
        if source == "marvis":
            with readonly_db(path) as conn:
                return int(conn.execute("SELECT count(*) FROM conversations").fetchone()[0])
        if source == "qclaw":
            sessions_path = path / "agents" / "main" / "sessions" / "sessions.json"
            sessions = json_value(sessions_path.read_text(encoding="utf-8"), {})
            return sum(
                1
                for key, value in sessions.items()
                if isinstance(value, dict)
                and value.get("sessionId")
                and not any(
                    marker in str(key)
                    for marker in (":heartbeat", ":cron:", ":dreaming-", ":subagent:")
                )
            )
        if source == "qoderwork":
            seen: set[str] = set()
            for db_path in _qoderwork_family_dbs(path):
                try:
                    with readonly_db(db_path) as conn:
                        for row in conn.execute(
                            """
                            SELECT id FROM chats
                            WHERE deleted_at IS NULL AND coalesce(chat_type,'task')='task'
                            """
                        ):
                            seen.add(str(row["id"]))
                except (OSError, sqlite3.DatabaseError):
                    continue
            return len(seen)
        if source == "zcode":
            with readonly_db(path) as conn:
                return int(
                    conn.execute(
                        """
                        SELECT count(*) FROM session
                        WHERE parent_id IS NULL
                        AND (time_archived IS NULL OR time_archived <= 0)
                        """
                    ).fetchone()[0]
                )
        if source in {"qoder", "qodercn"}:
            from agent_recovery.qoder import collect_index_sessions
            sessions, _warnings = collect_index_sessions(source, configured_index=path)
            return len(sessions)
        if source == "qwenworkcn":
            return len(_claude_transcript_files(path))
        if source == "grok":
            return len(_grok_summary_files(path))
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError):
        return 0
    return 0


def configured_extra_sources(
    config: dict[str, Any],
    *,
    with_counts: bool = True,
) -> dict[str, dict[str, Any]]:
    raw = config.get("extra_sources")
    raw = raw if isinstance(raw, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for source in EXTRA_SOURCES:
        value = raw.get(source)
        value = value if isinstance(value, dict) else {}
        path = Path(str(value.get("path") or "")).expanduser() if value.get("path") else None
        if source in {"qoder", "qodercn"}:
            preferred = next(
                (
                    item
                    for item in default_candidates(source)
                    if "chat_session" in sqlite_tables(item)
                ),
                None,
            )
            configured_valid = validate_source(source, path)[0] if path else False
            if preferred and (
                not configured_valid
                or (path is not None and path.name.casefold() == "state.vscdb")
            ):
                path = preferred
        if not path:
            path = next((item for item in default_candidates(source) if validate_source(source, item)[0]), None)
        valid, detail = validate_source(source, path) if path else (False, "未发现")
        if source not in raw:
            enabled = valid and source == "grok"
        else:
            enabled = bool(value.get("enabled", False))
        result[source] = {
            "enabled": enabled,
            "path": str(path or ""),
            "valid": valid,
            "detected": bool(path),
            "detail": detail,
            "label": SOURCE_LABELS[source],
            "conversations": estimate_conversations(source, path) if valid and with_counts else 0,
        }
    return result


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})")]


def quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _first_column(columns: Iterable[str], candidates: Iterable[str]) -> str:
    lookup = {str(column).casefold(): str(column) for column in columns}
    return next((lookup[name.casefold()] for name in candidates if name.casefold() in lookup), "")


def detect_custom_sqlite(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with readonly_db(path) as conn:
            tables = [
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            message_tables = sorted(
                tables,
                key=lambda name: (
                    name.casefold() not in {"messages", "chat_messages", "conversation_messages"},
                    name.casefold(),
                ),
            )
            for message_table in message_tables:
                message_columns = _sqlite_columns(conn, message_table)
                role = _first_column(
                    message_columns,
                    ("role", "sender_role", "author_role", "message_role", "author"),
                )
                content = _first_column(
                    message_columns,
                    ("content", "text", "body", "message", "searchable_text"),
                )
                conversation_id = _first_column(
                    message_columns,
                    (
                        "conversation_id", "session_id", "thread_id", "chat_id",
                        "conversationId", "sessionId", "threadId", "chatId",
                    ),
                )
                if not (role and content and conversation_id):
                    continue
                timestamp = _first_column(
                    message_columns,
                    ("created_at", "timestamp", "time", "updated_at", "createdAt"),
                )
                conversation_table = ""
                conversation_key = ""
                for table in sorted(
                    tables,
                    key=lambda name: (
                        name.casefold() not in {
                            "conversations", "sessions", "threads", "chats", "chat_sessions",
                        },
                        name.casefold(),
                    ),
                ):
                    if table == message_table:
                        continue
                    columns = _sqlite_columns(conn, table)
                    key = _first_column(
                        columns,
                        (
                            conversation_id, "id", "conversation_id", "session_id",
                            "thread_id", "chat_id", "uuid",
                        ),
                    )
                    if key:
                        conversation_table = table
                        conversation_key = key
                        break
                return {
                    "message_table": message_table,
                    "message_conversation_id": conversation_id,
                    "role": role,
                    "content": content,
                    "message_timestamp": timestamp,
                    "conversation_table": conversation_table,
                    "conversation_key": conversation_key,
                }
    except (OSError, sqlite3.DatabaseError):
        return {}
    return {}


def _custom_files(path: Path, pattern: str) -> list[Path]:
    if path.is_file():
        return [path] if path.match(pattern) else []
    if not path.is_dir():
        return []
    result: list[Path] = []
    for candidate in path.rglob(pattern):
        if not candidate.is_file():
            continue
        try:
            relative_parts = candidate.relative_to(path).parts[:-1]
        except ValueError:
            relative_parts = ()
        if any(part.casefold() in SKIP_DISCOVERY_DIRS for part in relative_parts):
            continue
        result.append(candidate)
    return result


def validate_custom_source(config: dict[str, Any], path: Path) -> tuple[bool, str]:
    format_name = str(config.get("format") or "").casefold()
    if format_name not in CUSTOM_FORMATS:
        return False, "请选择 JSONL、Markdown 或 SQLite"
    try:
        if format_name == "jsonl":
            count = len(_custom_files(path, "*.jsonl"))
            return (count > 0, f"{count} 个 JSONL 文件" if count else "未找到 JSONL 文件")
        if format_name == "markdown":
            count = 0
            for file_path in _custom_files(path, "*.md"):
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if MARKDOWN_ROLE_MARKER.search(text):
                    count += 1
            return (
                count > 0,
                (
                    f"{count} 个带用户/助手角色的 Markdown 对话"
                    if count
                    else "未找到带 User/Assistant 或 用户/助手标题的 Markdown 对话"
                ),
            )
        mapping = detect_custom_sqlite(path)
        return (
            bool(mapping),
            (
                f"SQLite：{mapping.get('conversation_table') or '无元数据表'}"
                f" + {mapping.get('message_table')}"
                if mapping
                else "未识别到会话ID、角色和正文字段"
            ),
        )
    except OSError:
        return False, "读取失败"


def estimate_custom_conversations(config: dict[str, Any], path: Path) -> int:
    format_name = str(config.get("format") or "").casefold()
    try:
        if format_name == "markdown":
            count = 0
            for file_path in _custom_files(path, "*.md"):
                try:
                    if MARKDOWN_ROLE_MARKER.search(
                        file_path.read_text(encoding="utf-8", errors="ignore")
                    ):
                        count += 1
                except OSError:
                    continue
            return count
        if format_name == "jsonl":
            session_ids: set[str] = set()
            for file_path in _custom_files(path, "*.jsonl"):
                with file_path.open("r", encoding="utf-8", errors="ignore") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except (ValueError, json.JSONDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        if any(
                            bool(event.get(key))
                            for key in ("isSidechain", "is_subagent", "isSubagent", "is_background")
                        ):
                            continue
                        event_type = str(event.get("type") or "").casefold()
                        if event_type in {
                            "system", "developer", "reasoning", "thinking", "tool", "tool_call",
                            "tool_result", "function_call", "function_call_result", "snapshot",
                        }:
                            continue
                        message = event.get("message") if isinstance(event.get("message"), dict) else {}
                        role = normalize_role(
                            message.get("role") or event.get("role") or event_type
                        )
                        if not role:
                            continue
                        session_ids.add(
                            str(
                                event.get("sessionId")
                                or event.get("session_id")
                                or event.get("conversationId")
                                or event.get("conversation_id")
                                or event.get("thread_id")
                                or event.get("chat_id")
                                or file_path.stem
                            )
                        )
            return len(session_ids)
        mapping = detect_custom_sqlite(path)
        if not mapping:
            return 0
        with readonly_db(path) as conn:
            column = quote_identifier(mapping["message_conversation_id"])
            table = quote_identifier(mapping["message_table"])
            return int(conn.execute(f"SELECT count(DISTINCT {column}) FROM {table}").fetchone()[0])
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError):
        return 0


def configured_custom_sources(
    config: dict[str, Any],
    *,
    with_counts: bool = True,
) -> dict[str, dict[str, Any]]:
    raw = config.get("custom_sources")
    raw = raw if isinstance(raw, list) else []
    result: dict[str, dict[str, Any]] = {}
    reserved = set(EXTRA_SOURCES) | {"all", "hermes", "codex", "workbuddy"}
    for value in raw[:50]:
        if not isinstance(value, dict):
            continue
        source = str(value.get("id") or "").casefold()
        if (
            not re.fullmatch(r"custom_[a-z0-9_]{1,48}", source)
            or source in reserved
            or source in result
        ):
            continue
        label = redact(value.get("label"), 80)
        format_name = str(value.get("format") or "").casefold()
        path = Path(str(value.get("path") or "")).expanduser()
        normalized = {
            "id": source,
            "label": label or "自定义 Agent",
            "format": format_name,
            "path": str(path) if str(value.get("path") or "") else "",
            "enabled": bool(value.get("enabled", False)),
        }
        valid, detail = (
            validate_custom_source(normalized, path)
            if normalized["path"]
            else (False, "尚未配置路径")
        )
        result[source] = {
            **normalized,
            "valid": valid,
            "detected": bool(normalized["path"]),
            "detail": detail,
            "conversations": (
                estimate_custom_conversations(normalized, path)
                if valid and with_counts
                else 0
            ),
        }
    return result


def _candidate_filenames(source: str) -> tuple[str, ...]:
    return {
        "claude": ("history.jsonl",),
        "cursor": ("conversation-search.db",),
        "qclaw": ("sessions.json",),
        "qoderwork": ("agents.db",),
        "zcode": ("db.sqlite",),
        "codepilot": ("chat.db", "data.db"),
        "marvis": ("state.db",),
    }.get(source, ())


def discover_in_roots(source: str, roots: Iterable[Path]) -> Path | None:
    filenames = {value.casefold() for value in _candidate_filenames(source)}
    for root in roots:
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in SKIP_DISCOVERY_DIRS and not name.startswith("$")
            ]
            for filename in files:
                if filename.casefold() not in filenames:
                    continue
                candidate = Path(current) / filename
                if validate_source(source, candidate)[0]:
                    return candidate.resolve()
    return None


def discover_extra_sources(
    config: dict[str, Any],
    extra_roots: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    selected = configured_extra_sources(config)
    roots = [Path(value).expanduser() for value in extra_roots if value and Path(value).expanduser().is_dir()]
    unresolved = {source for source in EXTRA_SOURCES if not selected[source]["valid"]}
    filename_sources: dict[str, set[str]] = {}
    for source in unresolved:
        for filename in _candidate_filenames(source):
            filename_sources.setdefault(filename.casefold(), set()).add(source)
    for root in roots:
        if not unresolved:
            break
        for current, dirs, files in os.walk(root):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in SKIP_DISCOVERY_DIRS and not name.startswith("$")
            ]
            for filename in files:
                possible = filename_sources.get(filename.casefold(), set()) & unresolved
                if not possible:
                    continue
                file_path = Path(current) / filename
                for source in tuple(possible):
                    candidates: list[Path] = [file_path]
                    if source == "cursor" and filename.casefold() == "conversation-search.db":
                        candidates.insert(0, file_path.parent)
                    elif source == "qclaw" and filename.casefold() == "sessions.json":
                        try:
                            candidates.insert(0, file_path.parents[3])
                        except IndexError:
                            pass
                    candidate = next(
                        (value for value in candidates if validate_source(source, value)[0]),
                        None,
                    )
                    if not candidate:
                        continue
                    valid, detail = validate_source(source, candidate)
                    selected[source].update(
                        {
                            "path": str(candidate.resolve()),
                            "valid": valid,
                            "detected": True,
                            "detail": detail,
                            "conversations": estimate_conversations(source, candidate),
                        }
                    )
                    unresolved.discard(source)
            if not unresolved:
                break
    return selected


def _claude_user_text(value: Any) -> str:
    text = content_text(value, "user")
    if not text:
        return ""
    if "<user_query>" in text:
        text = text.split("<user_query>", 1)[1].split("</user_query>", 1)[0]
    text = re.sub(
        r"(?is)<(?:system-reminder|local-command-caveat|task-notification|teammate-message)"
        r"\b[^>]*>.*?</(?:system-reminder|local-command-caveat|task-notification|teammate-message)>",
        "",
        text,
    ).strip()
    if text.casefold().startswith(
        (
            "<system-reminder",
            "<local-command-caveat",
            "<local-command-stdout",
            "<command-name",
            "<task-notification",
            "<teammate-message",
        )
    ):
        return ""
    return redact(text)


def _load_claude_history(path: Path) -> dict[str, dict[str, Any]]:
    history_path = path / "history.jsonl"
    result: dict[str, dict[str, Any]] = {}
    if not history_path.is_file():
        return result
    try:
        with history_path.open("r", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                session_id = str(event.get("sessionId") or event.get("session_id") or "")
                text = _claude_user_text(event.get("display"))
                if not session_id or not text:
                    continue
                timestamp = epoch(event.get("timestamp"))
                info = result.setdefault(
                    session_id,
                    {
                        "messages": [],
                        "seen": set(),
                        "cwd": "",
                        "title": "",
                        "created": 0.0,
                        "updated": 0.0,
                    },
                )
                dedupe = (text, timestamp)
                if dedupe in info["seen"]:
                    continue
                info["seen"].add(dedupe)
                if len(info["messages"]) < 5000:
                    info["messages"].append(
                        {"role": "user", "text": text, "timestamp": timestamp}
                    )
                info["cwd"] = str(event.get("project") or event.get("cwd") or info["cwd"])
                info["title"] = info["title"] or redact(text, 120)
                info["created"] = info["created"] or timestamp
                info["updated"] = max(float(info["updated"]), timestamp)
    except OSError:
        return {}
    return result


def _load_claude(
    path: Path, source: str = "claude"
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items_by_id: dict[str, dict[str, Any]] = {}
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    history = _load_claude_history(path)
    session_indexes = _claude_session_index_entries(path)
    for file_path in _claude_transcript_files(path):
        session_id = file_path.stem
        try:
            stat = file_path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_CLAUDE_TRANSCRIPT_BYTES:
            history_info = history.get(session_id, {})
            index_info = session_indexes.get(session_id, {})
            messages = list(history_info.get("messages") or [])
            if not messages and index_info.get("first_prompt"):
                messages = [
                    {
                        "role": "user",
                        "text": index_info["first_prompt"],
                        "timestamp": float(index_info.get("created") or 0),
                    }
                ]
            item = conversation(
                source,
                session_id,
                index_info.get("title")
                or history_info.get("title")
                or f"{SOURCE_LABELS.get(source, 'Claude')} 超大会话 {session_id[:8]}",
                messages,
                cwd=history_info.get("cwd") or index_info.get("cwd") or "",
                created_at=(
                    history_info.get("created")
                    or index_info.get("created")
                    or stat.st_ctime
                ),
                updated_at=(
                    history_info.get("updated")
                    or index_info.get("updated")
                    or stat.st_mtime
                ),
                model=index_info.get("model"),
                source_kind="claude-oversized-metadata-only",
                rollout_path=file_path,
            )
            items_by_id[session_id] = item
            if messages:
                messages_by_id[session_id] = messages
            continue
        messages: list[dict[str, Any]] = []
        cwd = ""
        model = ""
        title = ""
        created = 0.0
        updated = 0.0
        text_size = 0
        truncated = False
        seen_event_ids: set[str] = set()
        seen_messages: set[tuple[str, str, float]] = set()
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    session_id = str(event.get("sessionId") or session_id)
                    cwd = str(event.get("cwd") or cwd)
                    kind = str(event.get("type") or "").casefold()
                    if kind in {"custom-title", "custom_title"}:
                        title = redact(
                            event.get("customTitle") or event.get("title") or event.get("value"),
                            240,
                        ) or title
                        continue
                    if kind == "summary":
                        title = title or redact(event.get("summary"), 240)
                        continue
                    if event.get("isSidechain") or event.get("agentId"):
                        continue
                    message = event.get("message") if isinstance(event.get("message"), dict) else {}
                    role = str(message.get("role") or kind).casefold()
                    if role not in {"user", "assistant"}:
                        continue
                    event_id = str(event.get("uuid") or message.get("id") or "")
                    if event_id and event_id in seen_event_ids:
                        continue
                    text = (
                        _claude_user_text(message.get("content"))
                        if role == "user"
                        else content_text(message.get("content"), role)
                    )
                    if not text:
                        continue
                    timestamp = epoch(event.get("timestamp") or message.get("timestamp"))
                    dedupe = (role, text, timestamp)
                    if dedupe in seen_messages:
                        continue
                    seen_messages.add(dedupe)
                    if event_id:
                        seen_event_ids.add(event_id)
                    if len(messages) >= 20_000 or text_size + len(text) > 20_000_000:
                        truncated = True
                        break
                    messages.append({"role": role, "text": text, "timestamp": timestamp})
                    text_size += len(text)
                    model = str(message.get("model") or event.get("model") or model)
                    created = created or timestamp
                    updated = max(updated, timestamp)
        except OSError:
            continue
        if not messages:
            continue
        history_info = history.get(session_id, {})
        index_info = session_indexes.get(session_id, {})
        cwd = cwd or str(history_info.get("cwd") or index_info.get("cwd") or "")
        title = title or str(index_info.get("title") or history_info.get("title") or "")
        model = model or str(index_info.get("model") or "")
        record = conversation(
            source,
            session_id,
            title,
            messages,
            cwd=cwd,
            created_at=created or stat.st_ctime,
            updated_at=updated or stat.st_mtime,
            model=model,
            source_kind="claude-jsonl-partial" if truncated else "claude-jsonl",
            rollout_path=file_path,
        )
        existing = items_by_id.get(session_id)
        if not existing or record["message_count"] > existing["message_count"]:
            items_by_id[session_id] = record
            messages_by_id[session_id] = messages
    for session_id, info in history.items():
        if session_id in items_by_id:
            continue
        index_info = session_indexes.get(session_id, {})
        messages = list(info["messages"])
        items_by_id[session_id] = conversation(
            source,
            session_id,
            index_info.get("title") or info["title"],
            messages,
            cwd=info["cwd"] or index_info.get("cwd"),
            created_at=info["created"] or index_info.get("created"),
            updated_at=info["updated"] or index_info.get("updated"),
            model=index_info.get("model"),
            source_kind="claude-history-metadata-only",
            rollout_path=path / "history.jsonl",
        )
        messages_by_id[session_id] = messages
    for session_id, info in session_indexes.items():
        if session_id in items_by_id:
            continue
        messages = (
            [
                {
                    "role": "user",
                    "text": info["first_prompt"],
                    "timestamp": float(info.get("created") or 0),
                }
            ]
            if info.get("first_prompt")
            else []
        )
        items_by_id[session_id] = conversation(
            source,
            session_id,
            info["title"] or f"Claude 会话 {session_id[:8]}",
            messages,
            cwd=info["cwd"],
            created_at=info["created"],
            updated_at=info["updated"],
            model=info["model"],
            source_kind="claude-session-index-metadata-only",
            rollout_path=info["index_path"],
        )
        if messages:
            messages_by_id[session_id] = messages
    return list(items_by_id.values()), messages_by_id


def _codepilot_visible_text(value: Any, role: str = "assistant") -> str:
    return content_text(value, role)


def _load_codepilot(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    with readonly_db(path) as conn:
        for row in conn.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC"):
            rows = conn.execute(
                """
                SELECT role,content,created_at FROM messages
                WHERE session_id=? AND role IN ('user','assistant')
                  AND coalesce(is_heartbeat_ack,0)=0
                ORDER BY created_at,id
                """,
                (row["id"],),
            )
            messages = [
                {
                    "role": str(message["role"]),
                    "text": text,
                    "timestamp": epoch(message["created_at"]),
                }
                for message in rows
                if (text := _codepilot_visible_text(message["content"], str(message["role"])))
            ]
            if not any(message["role"] == "user" for message in messages):
                continue
            session_id = str(row["id"])
            messages_by_id[session_id] = messages
            cwd = row["sdk_cwd"] or row["working_directory"]
            items.append(
                conversation(
                    "codepilot", session_id, row["title"], messages, cwd=cwd,
                    created_at=row["created_at"], updated_at=row["updated_at"],
                    model=row["model"], source_kind="codepilot-sqlite",
                    rollout_path=path,
                )
            )
    return items, messages_by_id


def _load_cursor(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    search_db = path / "conversation-search.db"
    if not search_db.is_file():
        return items, messages_by_id
    with readonly_db(search_db) as conn:
        rows = conn.execute(
            """
            SELECT c.id,c.title,c.updated_at,c.is_archived,
                   coalesce(f.body,'') AS body
            FROM conversations c
            LEFT JOIN conversation_fts f ON f.rowid=c.fts_rowid
            WHERE c.source='local'
            ORDER BY c.updated_at DESC
            """
        )
        for row in rows:
            session_id = str(row["id"])
            body = content_text(row["body"], "user")
            messages = (
                [{"role": "user", "text": body, "timestamp": epoch(row["updated_at"])}]
                if body else []
            )
            if messages:
                messages_by_id[session_id] = messages
            items.append(
                conversation(
                    "cursor", session_id, row["title"], messages,
                    created_at=row["updated_at"], updated_at=row["updated_at"],
                    archived=bool(row["is_archived"]), source_kind=(
                        "cursor-local" if body else "cursor-metadata-only"
                    ), rollout_path=search_db,
                )
            )
    return items, messages_by_id


def _load_marvis(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    with readonly_db(path) as conn:
        for row in conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC"):
            rows = conn.execute(
                """
                SELECT role,content,created_at FROM messages
                WHERE conversation_id=? AND role IN ('user','assistant')
                ORDER BY message_seq,created_at
                """,
                (row["conversation_id"],),
            )
            messages = [
                {
                    "role": str(message["role"]),
                    "text": text,
                    "timestamp": epoch(message["created_at"]),
                }
                for message in rows
                if (text := content_text(message["content"], str(message["role"])))
            ]
            if not any(message["role"] == "user" for message in messages):
                continue
            session_id = str(row["conversation_id"])
            metadata = json_value(row["metadata"], {})
            cwd = metadata.get("cwd") or metadata.get("workspace") or ""
            messages_by_id[session_id] = messages
            items.append(
                conversation(
                    "marvis", session_id, row["title"], messages, cwd=cwd,
                    created_at=row["created_at"], updated_at=row["updated_at"],
                    model=metadata.get("model"), source_kind="marvis-sqlite",
                    rollout_path=path,
                )
            )
    return items, messages_by_id


def _load_qclaw(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    sessions_path = path / "agents" / "main" / "sessions" / "sessions.json"
    sessions = json_value(sessions_path.read_text(encoding="utf-8"), {})
    for session_key, metadata in sessions.items():
        if not isinstance(metadata, dict) or any(
            marker in session_key
            for marker in (":heartbeat", ":cron:", ":dreaming-", ":subagent:")
        ):
            continue
        session_id = str(metadata.get("sessionId") or "")
        session_file = Path(str(metadata.get("sessionFile") or ""))
        if not session_file.is_absolute():
            session_file = sessions_path.parent / session_file
        if (
            not session_id
            or not session_file.is_file()
            or session_file.name.endswith(".trajectory.jsonl")
        ):
            continue
        messages: list[dict[str, Any]] = []
        cwd = ""
        try:
            with session_file.open("r", encoding="utf-8", errors="ignore") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if event.get("type") == "session":
                        cwd = str(event.get("cwd") or cwd)
                        continue
                    if event.get("type") != "message":
                        continue
                    message = event.get("message") if isinstance(event.get("message"), dict) else {}
                    role = str(message.get("role") or "").casefold()
                    if role not in {"user", "assistant"}:
                        continue
                    text = content_text(message.get("content"), role)
                    if not text:
                        continue
                    messages.append(
                        {"role": role, "text": text, "timestamp": epoch(event.get("timestamp"))}
                    )
        except OSError:
            continue
        if not any(message["role"] == "user" for message in messages):
            continue
        messages_by_id[session_id] = messages
        title = metadata.get("label") or metadata.get("displayName") or ""
        items.append(
            conversation(
                "qclaw", session_id, title, messages, cwd=cwd,
                created_at=metadata.get("startedAt") or metadata.get("sessionStartedAt"),
                updated_at=metadata.get("updatedAt"), model=metadata.get("model"),
                source_kind=str(metadata.get("chatType") or "qclaw-jsonl"),
                rollout_path=session_file,
            )
        )
    return items, messages_by_id


def _qoderwork_family_dbs(path: Path) -> list[Path]:
    """Return the given agents.db plus any sibling database from the renamed
    product family (Qoder -> QoderWork -> 千问办公/QwenWork) that lives in the
    same AppData/Application Support root. Only the standard product directory
    names trigger the sibling scan; a user-picked custom path loads alone."""
    result = [path]
    try:
        app_dir = path.parent.parent
        root = app_dir.parent
    except (AttributeError, ValueError):
        return result
    if app_dir.name not in QODERWORK_FAMILY_DIRS or not root.is_dir():
        return result
    required = {"projects", "chats", "sub_chats", "messages"}
    for name in QODERWORK_FAMILY_DIRS:
        if name == app_dir.name:
            continue
        candidate = root / name / path.parent.name / path.name
        if not candidate.is_file():
            continue
        try:
            if required.issubset(sqlite_tables(candidate)):
                result.append(candidate)
        except (OSError, sqlite3.DatabaseError):
            continue
    return result


def _load_qoderwork_db(
    conn: sqlite3.Connection,
    db_path: Path,
    items: list[dict[str, Any]],
    messages_by_id: dict[str, list[dict[str, Any]]],
    seen_ids: set[str],
) -> None:
    rows = conn.execute(
        """
        SELECT c.*,p.name AS project_name,p.path AS project_path
        FROM chats c JOIN projects p ON p.id=c.project_id
        WHERE c.deleted_at IS NULL AND coalesce(c.chat_type,'task')='task'
        ORDER BY c.updated_at DESC
        """
    ).fetchall()
    for row in rows:
        session_id = str(row["id"])
        if session_id in seen_ids:
            continue
        message_rows = conn.execute(
            """
            SELECT role,searchable_text,parts,created_at
            FROM messages
            WHERE chat_id=? AND role IN ('user','assistant')
            ORDER BY sequence,created_at
            """,
            (row["id"],),
        )
        messages: list[dict[str, Any]] = []
        for message in message_rows:
            role = str(message["role"] or "")
            parts = json_value(message["parts"], [])
            text = content_text(parts, role)
            if not text:
                text = content_text(message["searchable_text"], role)
            if text:
                messages.append(
                    {
                        "role": role,
                        "text": text,
                        "timestamp": epoch(message["created_at"]),
                    }
                )
        if not any(message["role"] == "user" for message in messages):
            continue
        seen_ids.add(session_id)
        messages_by_id[session_id] = messages
        cwd = row["worktree_path"] or row["project_path"]
        items.append(
            conversation(
                "qoderwork", session_id, row["name"], messages, cwd=cwd,
                created_at=row["created_at"], updated_at=row["updated_at"],
                source_kind=str(row["source"] or "qoderwork-sqlite"),
                rollout_path=db_path,
            )
        )


def _load_qoderwork(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for db_path in _qoderwork_family_dbs(path):
        try:
            with readonly_db(db_path) as conn:
                _load_qoderwork_db(conn, db_path, items, messages_by_id, seen_ids)
        except (OSError, sqlite3.DatabaseError):
            continue
    return items, messages_by_id


def normalize_role(value: Any) -> str:
    role = str(value or "").casefold().strip()
    if role in {"user", "human", "用户", "person"}:
        return "user"
    if role in {"assistant", "ai", "bot", "助手", "model"}:
        return "assistant"
    return ""


def custom_content(value: Any) -> str:
    return content_text(value)


def _load_custom_jsonl(
    source: str,
    config: dict[str, Any],
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    root = path if path.is_dir() else path.parent
    for file_path in _custom_files(path, "*.jsonl"):
        fallback_id = (
            file_path.relative_to(root).with_suffix("").as_posix()
            if path.is_dir()
            else file_path.stem
        )
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict) or any(
                        bool(event.get(key))
                        for key in ("isSidechain", "is_subagent", "isSubagent", "is_background")
                    ):
                        continue
                    event_type = str(event.get("type") or "").casefold()
                    if event_type in {
                        "system", "developer", "reasoning", "thinking", "tool", "tool_call",
                        "tool_result", "function_call", "function_call_result", "snapshot",
                    }:
                        continue
                    message = event.get("message") if isinstance(event.get("message"), dict) else {}
                    role = normalize_role(message.get("role") or event.get("role") or event_type)
                    if not role:
                        continue
                    text = custom_content(
                        message.get("content")
                        if "content" in message
                        else event.get("content", event.get("text"))
                    )
                    if not text:
                        continue
                    session_id = str(
                        event.get("sessionId")
                        or event.get("session_id")
                        or event.get("conversationId")
                        or event.get("conversation_id")
                        or event.get("threadId")
                        or event.get("thread_id")
                        or event.get("chatId")
                        or event.get("chat_id")
                        or fallback_id
                    )
                    timestamp = epoch(
                        event.get("timestamp")
                        or event.get("created_at")
                        or event.get("createdAt")
                        or message.get("timestamp")
                    )
                    grouped.setdefault(session_id, []).append(
                        {"role": role, "text": text, "timestamp": timestamp}
                    )
                    info = metadata.setdefault(
                        session_id,
                        {"path": file_path, "title": "", "cwd": "", "model": ""},
                    )
                    info["title"] = (
                        event.get("title")
                        or event.get("aiTitle")
                        or message.get("title")
                        or info["title"]
                    )
                    info["cwd"] = (
                        event.get("cwd")
                        or event.get("workspace")
                        or event.get("project_path")
                        or info["cwd"]
                    )
                    info["model"] = message.get("model") or event.get("model") or info["model"]
        except OSError:
            continue
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    for session_id, messages in grouped.items():
        messages.sort(key=lambda item: float(item["timestamp"] or 0))
        if not any(message["role"] == "user" for message in messages):
            continue
        info = metadata[session_id]
        messages_by_id[session_id] = messages
        items.append(
            conversation(
                source,
                session_id,
                info["title"],
                messages,
                cwd=info["cwd"],
                model=info["model"],
                source_kind="custom-jsonl",
                rollout_path=info["path"],
            )
        )
    return items, messages_by_id


def _markdown_messages(text: str, timestamp: float) -> tuple[str, list[dict[str, Any]]]:
    body = text.replace("\x00", "")
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2].lstrip()
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    title = redact(title_match.group(1), 240) if title_match else ""
    matches = list(MARKDOWN_ROLE_MARKER.finditer(body))
    messages: list[dict[str, Any]] = []
    if not matches:
        return title, messages
    for index, match in enumerate(matches):
        role = normalize_role(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = redact(body[start:end])
        if role and value:
            messages.append(
                {"role": role, "text": value, "timestamp": timestamp + index / 1000}
            )
    return title, messages


def _load_custom_markdown(
    source: str,
    config: dict[str, Any],
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    root = path if path.is_dir() else path.parent
    for file_path in _custom_files(path, "*.md"):
        try:
            timestamp = file_path.stat().st_mtime
            title, messages = _markdown_messages(
                file_path.read_text(encoding="utf-8", errors="ignore"),
                timestamp,
            )
        except OSError:
            continue
        if not any(message["role"] == "user" for message in messages):
            continue
        session_id = (
            file_path.relative_to(root).with_suffix("").as_posix()
            if path.is_dir()
            else file_path.stem
        )
        messages_by_id[session_id] = messages
        items.append(
            conversation(
                source,
                session_id,
                title or file_path.stem,
                messages,
                cwd=file_path.parent,
                created_at=timestamp,
                updated_at=timestamp,
                source_kind="custom-markdown",
                rollout_path=file_path,
            )
        )
    return items, messages_by_id


def _row_value(row: dict[str, Any], aliases: Iterable[str]) -> Any:
    lookup = {key.casefold(): value for key, value in row.items()}
    return next((lookup[name.casefold()] for name in aliases if name.casefold() in lookup), "")


def _load_custom_sqlite(
    source: str,
    config: dict[str, Any],
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    mapping = detect_custom_sqlite(path)
    if not mapping:
        return [], {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    conversation_rows: dict[str, dict[str, Any]] = {}
    with readonly_db(path) as conn:
        if mapping["conversation_table"]:
            table = quote_identifier(mapping["conversation_table"])
            key = mapping["conversation_key"]
            for row in conn.execute(f"SELECT * FROM {table}"):
                value = dict(row)
                session_id = str(value.get(key) or "")
                if session_id:
                    conversation_rows[session_id] = value
        select = [
            f"{quote_identifier(mapping['message_conversation_id'])} AS conversation_id",
            f"{quote_identifier(mapping['role'])} AS role",
            f"{quote_identifier(mapping['content'])} AS content",
        ]
        if mapping["message_timestamp"]:
            select.append(
                f"{quote_identifier(mapping['message_timestamp'])} AS message_timestamp"
            )
        else:
            select.append("0 AS message_timestamp")
        query = f"SELECT {', '.join(select)} FROM {quote_identifier(mapping['message_table'])}"
        if mapping["message_timestamp"]:
            query += f" ORDER BY {quote_identifier(mapping['message_timestamp'])}"
        for row in conn.execute(query):
            role = normalize_role(row["role"])
            text = custom_content(row["content"])
            session_id = str(row["conversation_id"] or "")
            if role and text and session_id:
                grouped.setdefault(session_id, []).append(
                    {
                        "role": role,
                        "text": text,
                        "timestamp": epoch(row["message_timestamp"]),
                    }
                )
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    for session_id, messages in grouped.items():
        if not any(message["role"] == "user" for message in messages):
            continue
        metadata = conversation_rows.get(session_id, {})
        if any(
            bool(_row_value(metadata, aliases))
            for aliases in (
                ("is_subagent", "isSidechain", "is_background_automation", "is_background"),
                ("parent_session_id", "parent_thread_id"),
            )
        ):
            continue
        title = _row_value(metadata, ("title", "name", "custom_title", "subject"))
        cwd = _row_value(
            metadata,
            ("cwd", "workspace", "working_directory", "project_path", "worktree_path"),
        )
        created = _row_value(metadata, ("created_at", "createdAt", "started_at"))
        updated = _row_value(
            metadata,
            ("updated_at", "updatedAt", "last_activity_at", "ended_at"),
        )
        model = _row_value(metadata, ("model", "model_id", "model_name", "provider"))
        messages_by_id[session_id] = messages
        items.append(
            conversation(
                source,
                session_id,
                title,
                messages,
                cwd=cwd,
                created_at=created,
                updated_at=updated,
                model=model,
                source_kind="custom-sqlite",
                rollout_path=path,
            )
        )
    return items, messages_by_id


def load_custom_source(
    source: str,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], str]:
    path = Path(str(config.get("path") or "")).expanduser()
    valid, detail = validate_custom_source(config, path)
    if not valid:
        return [], {}, detail
    loader = {
        "jsonl": _load_custom_jsonl,
        "markdown": _load_custom_markdown,
        "sqlite": _load_custom_sqlite,
    }.get(str(config.get("format") or "").casefold())
    if not loader:
        return [], {}, "不支持的数据格式"
    try:
        items, messages = loader(source, config, path)
        return items, messages, ""
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
        return [], {}, f"{type(exc).__name__}: {exc}"


def _zcode_text_parts(conn: sqlite3.Connection, message_id: Any, role: str = "assistant") -> str:
    texts: list[str] = []
    for row in conn.execute(
        "SELECT data FROM part WHERE message_id=? ORDER BY sequence",
        (message_id,),
    ):
        payload = json_value(row["data"], {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get("type") or "").casefold() != "text":
            continue
        text = str(payload.get("text") or "").strip()
        if text:
            texts.append(text)
    return content_text("\n\n".join(texts), role)


def _load_zcode(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    with readonly_db(path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, directory, time_created, time_updated
            FROM session
            WHERE parent_id IS NULL
            AND (time_archived IS NULL OR time_archived <= 0)
            ORDER BY time_updated DESC
            """
        ).fetchall()
        for row in rows:
            message_rows = conn.execute(
                "SELECT id, data, time_created FROM message WHERE session_id=? ORDER BY sequence",
                (row["id"],),
            )
            messages: list[dict[str, Any]] = []
            model = ""
            for message in message_rows:
                payload = json_value(message["data"], {})
                if not isinstance(payload, dict):
                    continue
                role = str(payload.get("role") or "").casefold()
                semantics = payload.get("semantics")
                semantics = semantics if isinstance(semantics, dict) else {}
                origin = str(semantics.get("origin") or "")
                # 只要真人输入与助手正式回复：
                # 排除 todo_reminder / background_notification / timeline_event 等注入内容
                if role == "user" and origin == "real_user":
                    role = "user"
                elif role == "assistant" and origin != "system":
                    role = "assistant"
                else:
                    continue
                text = _zcode_text_parts(conn, message["id"], role)
                if not text:
                    continue
                if role == "assistant" and not model:
                    model = redact(payload.get("modelID") or payload.get("model_id"), 120)
                messages.append(
                    {
                        "role": role,
                        "text": text,
                        "timestamp": epoch(message["time_created"]),
                    }
                )
            if not any(message["role"] == "user" for message in messages):
                continue
            session_id = str(row["id"])
            messages_by_id[session_id] = messages
            items.append(
                conversation(
                    "zcode", session_id, row["title"], messages,
                    cwd=row["directory"],
                    created_at=row["time_created"], updated_at=row["time_updated"],
                    model=model,
                    source_kind="zcode-cli",
                    rollout_path=path,
                )
            )
    return items, messages_by_id



def _load_qoder_family(
    source: str, path: Path, transcripts_root: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    from agent_recovery.qoder import recover_all

    isolated = transcripts_root is not None
    recovered, _warnings = recover_all(
        source,
        configured_index=path,
        compact_root=transcripts_root if isolated else None,
        full_root=transcripts_root if isolated else None,
        include_messages=True,
        include_preview=False,
        use_default_indexes=not isolated,
    )
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    for session in recovered:
        messages = [
            {
                "role": message["role"],
                "text": message["text"],
                "timestamp": message.get("timestamp") or 0,
                "line": (message.get("evidence") or {}).get("line"),
                "event_id": str((message.get("evidence") or {}).get("event_id") or ""),
            }
            for message in session.messages
        ]
        if messages:
            messages_by_id[session.session_id] = messages
        source_kind = f"{source}-ide"
        if session.source_kind == "metadata_only":
            source_kind += "-metadata-only"
        elif session.index_kind == "shared-client-cache":
            source_kind += "-transcript"
        items.append(
            conversation(
                source,
                session.session_id,
                session.title,
                messages,
                cwd=session.cwd,
                created_at=session.created,
                updated_at=session.updated,
                source_kind=source_kind,
                rollout_path=str(session.selected_path or path),
            )
        )
    return items, messages_by_id


def _load_qoder(path: Path):
    return _load_qoder_family("qoder", path)


def _load_qodercn(path: Path):
    return _load_qoder_family("qodercn", path)


def _grok_summary_files(path: Path) -> list[Path]:
    from agent_recovery.grok import summary_files

    return summary_files(path)


def _load_grok(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    from agent_recovery.grok import recover_all

    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    for session in recover_all(path, include_messages=True, include_preview=False):
        messages = [
            {
                "role": message["role"],
                "text": message["text"],
                "timestamp": message.get("timestamp") or 0,
                "line": (message.get("evidence") or {}).get("line"),
                "event_id": str((message.get("evidence") or {}).get("event_id") or ""),
            }
            for message in session.messages
        ]
        if messages:
            messages_by_id[session.session_id] = messages
        items.append(
            conversation(
                "grok",
                session.session_id,
                session.title,
                messages,
                cwd=session.cwd,
                created_at=session.created,
                updated_at=session.updated,
                model=session.model,
                source_kind="grok-updates" if session.source_kind == "transcript" else "grok-metadata-only",
                rollout_path=str(session.selected_path or path),
            )
        )
    return items, messages_by_id


def _load_qwenworkcn(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    # 千问办公会话与 Claude Code 同构（projects/<编码目录>/<会话>.jsonl），复用其解析器
    items, messages_by_id = _load_claude(path, "qwenworkcn")
    # 千问办公 CLI 会在用户消息前缀注入"当前目录；"，展示标题时剥掉（与客户端一致）
    prefix = re.compile(r"^[A-Za-z]:\\[^；;\n]+[；;]\s*")
    kept: list[dict[str, Any]] = []
    for item in items:
        for key in ("title", "preview"):
            cleaned = prefix.sub("", item.get(key) or "")
            if cleaned:
                item[key] = cleaned
        # 跳过客户端自动注入的记忆反思会话（非真人对话）
        if item["title"].startswith("Target file this round:"):
            messages_by_id.pop(item["id"], None)
            continue
        kept.append(item)
    return kept, messages_by_id


def _load_qwenworkcn_legacy(path: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    items: list[dict[str, Any]] = []
    messages_by_id: dict[str, list[dict[str, Any]]] = {}
    for conv_file in sorted(path.glob("workspace/*/conversations.json")):
        data = json_value(conv_file.read_text(encoding="utf-8", errors="replace"), {})
        if not isinstance(data, dict):
            continue
        for conv_id, conv in data.items():
            if not isinstance(conv, dict):
                continue
            mapping = conv.get("mapping")
            mapping = mapping if isinstance(mapping, dict) else {}
            nodes = [node for node in mapping.values() if isinstance(node, dict)]
            nodes.sort(key=lambda node: epoch((node.get("message") or {}).get("create_time")))
            messages: list[dict[str, Any]] = []
            for node in nodes:
                message = node.get("message")
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "").casefold()
                if role not in {"user", "assistant"}:
                    continue
                content = message.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if isinstance(parts, list):
                    text = content_text(parts, role)
                else:
                    text = content_text(content, role)
                if not text:
                    continue
                messages.append(
                    {"role": role, "text": text, "timestamp": epoch(message.get("create_time"))}
                )
            if not any(message["role"] == "user" for message in messages):
                continue
            cid = f"{conv_file.parent.name}:{conv_id}"
            messages_by_id[cid] = messages
            items.append(
                conversation(
                    "qwenworkcn", cid, conv.get("title"), messages,
                    cwd=str(conv_file.parent),
                    created_at=conv.get("create_time"), updated_at=conv.get("update_time"),
                    source_kind="qwenworkcn-cli",
                    rollout_path=str(conv_file),
                )
            )
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items, messages_by_id


LOADERS = {
    "claude": _load_claude,
    "cursor": _load_cursor,
    "qclaw": _load_qclaw,
    "qoderwork": _load_qoderwork,
    "zcode": _load_zcode,
    "codepilot": _load_codepilot,
    "marvis": _load_marvis,
    "qoder": _load_qoder,
    "qodercn": _load_qodercn,
    "qwenworkcn": _load_qwenworkcn,
    "grok": _load_grok,
}


def load_extra_source(
    source: str,
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], str]:
    valid, detail = validate_source(source, path)
    if not valid:
        return [], {}, detail
    try:
        items, messages = LOADERS[source](path)
        return items, messages, ""
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as exc:
        return [], {}, f"{type(exc).__name__}: {exc}"
