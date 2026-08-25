"""Qoder / QoderCN recovery: index + all plaintext candidates + coverage pick."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .common import (
    epoch,
    parse_transcript,
    rank_candidates,
    redact_preview,
    sqlite_ro,
    sqlite_tables,
)

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
SAFE_SESSION_COLUMNS = (
    "session_id",
    "session_title",
    "project_uri",
    "project_name",
    "gmt_create",
    "gmt_modified",
    "session_type",
    "mode",
)
SELECTION_RULE = (
    "highest parsed user+assistant message count; then last evidence line and mtime"
)
COMPACT_STEM_MIN = 6
COMPACT_STEM_MAX = 24


@dataclass(frozen=True)
class ProductLayout:
    product: str
    index_dbs: tuple[Path, ...]
    transcript_roots: tuple[tuple[Path, str], ...]

    def discovery_locations(self) -> list[dict[str, Any]]:
        locations = [
            {
                "role": "session_index",
                "path": self.index_dbs[0],
                "storage": "sqlite",
                "evidence": True,
            }
        ]
        for extra in self.index_dbs[1:]:
            locations.append(
                {
                    "role": "legacy_index",
                    "path": extra,
                    "storage": "vscode-sqlite",
                    "evidence": True,
                }
            )
        for path, kind in self.transcript_roots:
            role = (
                "compact_transcript_root"
                if kind == "compact_transcript"
                else "full_transcript_root"
            )
            locations.append(
                {"role": role, "path": path, "storage": "jsonl", "evidence": True}
            )
        return locations


def _layouts() -> dict[str, ProductLayout]:
    return {
        "qoder": ProductLayout(
            product="qoder",
            index_dbs=(
                APPDATA / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db",
                APPDATA / "Qoder" / "User" / "globalStorage" / "state.vscdb",
            ),
            transcript_roots=(
                (HOME / ".qoder" / "cache" / "projects", "compact_transcript"),
                (HOME / ".qoder" / "projects", "full_transcript"),
            ),
        ),
        "qodercn": ProductLayout(
            product="qodercn",
            index_dbs=(
                APPDATA / "QoderCN" / "SharedClientCache" / "cache" / "db" / "local.db",
                APPDATA / "QoderCN" / "User" / "globalStorage" / "state.vscdb",
            ),
            transcript_roots=(
                (HOME / ".qoder-cn" / "cache" / "projects", "compact_transcript"),
                (HOME / ".qoder-cn" / "projects", "full_transcript"),
                (
                    APPDATA / "QoderCN" / "SharedClientCache" / "cli" / "projects",
                    "full_transcript",
                ),
            ),
        ),
    }


def layout(product: str) -> ProductLayout:
    layouts = _layouts()
    if product not in layouts:
        raise KeyError(f"Unsupported Qoder product: {product}")
    return layouts[product]


def known_products() -> tuple[str, ...]:
    return tuple(_layouts())


@dataclass
class RecoveredSession:
    session_id: str
    title: str = ""
    cwd: str = ""
    project_name: str = ""
    created: float = 0.0
    updated: float = 0.0
    session_type: str = ""
    mode: str = ""
    index_kind: str = ""
    source_kind: str = "metadata_only"
    selected_path: Path | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    candidate_count: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def coverage(self) -> dict[str, int]:
        return {
            "message_count": len(self.messages),
            "user_messages": sum(item["role"] == "user" for item in self.messages),
            "assistant_messages": sum(item["role"] == "assistant" for item in self.messages),
        }


def read_local_db_sessions(index_db: Path) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if not index_db.is_file():
        return [], [f"Session index not found: {index_db}"]
    try:
        conn = sqlite_ro(index_db)
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_session'"
            ).fetchone()
            if not table:
                return [], ["chat_session table is absent; Qoder schema may have changed."]
            columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_session)")}
            selected = [name for name in SAFE_SESSION_COLUMNS if name in columns]
            if "session_id" not in selected:
                return [], ["chat_session.session_id is absent; mapping cannot continue safely."]
            order = "gmt_modified DESC" if "gmt_modified" in columns else "rowid DESC"
            sql = f"SELECT {', '.join(selected)} FROM chat_session ORDER BY {order}"
            rows = []
            for raw in conn.execute(sql):
                row = dict(raw)
                rows.append(
                    {
                        "id": str(row.get("session_id") or "").strip(),
                        "session_id": str(row.get("session_id") or "").strip(),
                        "title": str(row.get("session_title") or ""),
                        "session_title": str(row.get("session_title") or ""),
                        "cwd": str(row.get("project_uri") or ""),
                        "project_uri": str(row.get("project_uri") or ""),
                        "project_name": str(row.get("project_name") or ""),
                        "created": epoch(row.get("gmt_create")),
                        "updated": epoch(row.get("gmt_modified")),
                        "gmt_create": row.get("gmt_create"),
                        "gmt_modified": row.get("gmt_modified"),
                        "session_type": str(row.get("session_type") or ""),
                        "mode": str(row.get("mode") or ""),
                        "index_kind": "shared-client-cache",
                    }
                )
            omitted = [name for name in SAFE_SESSION_COLUMNS if name not in columns]
            if omitted:
                warnings.append("Optional chat_session columns absent: " + ", ".join(omitted))
            return [row for row in rows if row["id"]], warnings
        finally:
            conn.close()
    except Exception as exc:
        return [], [f"Could not read session index in read-only mode: {type(exc).__name__}: {exc}"]


def read_vscdb_sessions(index_db: Path) -> list[dict[str, Any]]:
    if not index_db.is_file() or "ItemTable" not in sqlite_tables(index_db):
        return []
    sessions: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite_ro(index_db)
        try:
            rows = conn.execute(
                "SELECT value FROM ItemTable WHERE key LIKE 'lingma.chat.localHistory.%'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    for row in rows:
        try:
            entries = json.loads(row[0] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            session_id = str(entry.get("sessionId") or entry.get("id") or "").strip()
            if not session_id:
                continue
            stamp = epoch(entry.get("timestamp"))
            title = str(entry.get("title") or "")
            slot = sessions.setdefault(
                session_id,
                {
                    "id": session_id,
                    "session_id": session_id,
                    "title": "",
                    "session_title": "",
                    "cwd": "",
                    "project_uri": "",
                    "project_name": "",
                    "created": 0.0,
                    "updated": 0.0,
                    "session_type": "quest",
                    "mode": "",
                    "index_kind": "legacy-vscdb",
                },
            )
            if title and not slot["title"]:
                slot["title"] = title
                slot["session_title"] = title
            if stamp and (not slot["created"] or stamp < slot["created"]):
                slot["created"] = stamp
            if stamp > slot["updated"]:
                slot["updated"] = stamp
    return sorted(sessions.values(), key=lambda item: item["updated"], reverse=True)


def merge_session_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        session_id = str(row.get("id") or row.get("session_id") or "")
        if not session_id:
            continue
        current = by_id.get(session_id)
        if not current:
            by_id[session_id] = dict(row)
            continue
        prefer = row.get("index_kind") == "shared-client-cache"
        for field_name in ("title", "session_title", "cwd", "project_uri", "project_name", "session_type", "mode", "index_kind"):
            if row.get(field_name) and (prefer or not current.get(field_name)):
                current[field_name] = row[field_name]
        stamps = [float(value or 0) for value in (current.get("created"), row.get("created")) if value]
        if stamps:
            current["created"] = min(stamps)
        current["updated"] = max(float(current.get("updated") or 0), float(row.get("updated") or 0))
    return sorted(by_id.values(), key=lambda item: float(item.get("updated") or 0), reverse=True)


def collect_index_sessions(
    product: str,
    *,
    configured_index: Path | None = None,
    use_default_indexes: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    product_layout = layout(product)
    paths: list[Path] = []
    if configured_index is not None:
        paths.append(configured_index)
    if use_default_indexes or configured_index is None:
        paths.extend(product_layout.index_dbs)
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in paths:
        key = str(path.resolve(strict=False)).casefold()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        tables = sqlite_tables(path)
        if "chat_session" in tables:
            found, found_warnings = read_local_db_sessions(path)
            rows.extend(found)
            warnings.extend(found_warnings)
        elif "ItemTable" in tables:
            rows.extend(read_vscdb_sessions(path))
    return merge_session_rows(rows), warnings


def _iter_jsonl(root: Path, max_files: int) -> Iterable[Path]:
    if not root.is_dir():
        return
    search_roots = [root]
    for child in (root / "projects", root / "cache" / "projects"):
        if child.is_dir() and child not in search_roots:
            search_roots.append(child)
    patterns = (
        "*/transcript/*.jsonl",
        "*/conversation-history/*/*.jsonl",
        "transcript/*.jsonl",
        "conversation-history/*/*.jsonl",
        "*.jsonl",
    )
    seen = 0
    emitted: set[str] = set()
    for search_root in search_roots:
        for pattern in patterns:
            try:
                matches = search_root.glob(pattern)
            except OSError:
                continue
            for path in matches:
                key = str(path.resolve(strict=False)).casefold()
                if key in emitted or not path.is_file():
                    continue
                emitted.add(key)
                yield path
                seen += 1
                if seen >= max_files:
                    return


def build_transcript_index(
    roots: list[tuple[Path, str]],
    *,
    max_files: int,
) -> dict[str, list[tuple[Path, str]]]:
    index: dict[str, list[tuple[Path, str]]] = {}
    remaining = max_files
    for root, source_kind in roots:
        if remaining <= 0:
            break
        for path in _iter_jsonl(root, remaining):
            index.setdefault(path.stem.casefold(), []).append((path, source_kind))
            remaining -= 1
    return index


def match_transcripts(
    session_id: str,
    index: dict[str, list[tuple[Path, str]]],
) -> list[tuple[Path, str, str]]:
    session_key = session_id.casefold()
    found: dict[str, tuple[Path, str, str]] = {}
    for stem, items in index.items():
        exact = stem == session_key
        prefix = COMPACT_STEM_MIN <= len(stem) <= COMPACT_STEM_MAX and session_key.startswith(stem)
        if not (exact or prefix):
            continue
        match = "exact" if exact else "session_prefix"
        for path, source_kind in items:
            found[str(path.resolve(strict=False)).casefold()] = (path, source_kind, match)
    return list(found.values())


def _stat_file(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return 0, 0


def evaluate_candidates(
    session_id: str,
    index: dict[str, list[tuple[Path, str]]],
    *,
    include_messages: bool,
    include_preview: bool,
    preview_chars: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path, source_kind, match in match_transcripts(session_id, index):
        parsed = parse_transcript(path)
        size, modified_ns = _stat_file(path)
        item: dict[str, Any] = {
            "path": str(path.resolve(strict=False)),
            "source_kind": source_kind,
            "match": match,
            "status": parsed["status"],
            "message_count": parsed["message_count"],
            "user_messages": parsed["user_messages"],
            "assistant_messages": parsed["assistant_messages"],
            "invalid_lines": parsed["invalid_lines"],
            "first_evidence": parsed["first_evidence"],
            "last_evidence": parsed["last_evidence"],
            "cwd": parsed.get("cwd") or "",
            "bytes": size,
            "modified_ns": modified_ns,
        }
        if parsed.get("error"):
            item["error"] = parsed["error"]
        if include_messages:
            item["messages"] = parsed["messages"]
        if include_preview and parsed["messages"]:
            messages = parsed["messages"]
            first_user = next((row for row in messages if row["role"] == "user"), None)
            latest_user_index = max(
                (index_i for index_i, row in enumerate(messages) if row["role"] == "user"),
                default=-1,
            )
            latest_user = messages[latest_user_index] if latest_user_index >= 0 else None
            latest_assistant = next(
                (
                    row
                    for row in reversed(messages[latest_user_index + 1 :])
                    if row["role"] == "assistant"
                ),
                None,
            )

            def preview(row: dict[str, Any] | None) -> dict[str, Any] | None:
                if not row:
                    return None
                return {
                    "text": redact_preview(row["text"], preview_chars),
                    "evidence": row["evidence"],
                }

            item["preview"] = {
                "original_goal": preview(first_user),
                "latest_user": preview(latest_user),
                "latest_assistant_after_user": preview(latest_assistant),
            }
        results.append(item)
    return rank_candidates(results)


def resolve_transcript_roots(
    product: str,
    *,
    compact_root: Path | None = None,
    full_root: Path | None = None,
    extra_roots: Iterable[Path] | None = None,
) -> list[tuple[Path, str]]:
    if compact_root is not None or full_root is not None:
        roots: list[tuple[Path, str]] = []
        if compact_root is not None:
            roots.append((compact_root, "compact_transcript"))
        if full_root is not None:
            roots.append((full_root, "full_transcript"))
        return roots
    roots = list(layout(product).transcript_roots)
    for extra in extra_roots or ():
        if extra and extra not in {path for path, _ in roots}:
            roots.append((extra, "extra_transcript"))
    return roots


def _session_from_row(
    row: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    include_messages: bool,
) -> RecoveredSession:
    chosen = candidates[0] if candidates else None
    messages = list((chosen or {}).get("messages") or []) if include_messages else []
    if chosen and not include_messages:
        messages = []
    source_kind = "transcript" if chosen and chosen.get("message_count") else "metadata_only"
    if row.get("source_kind") == "transcript_only" and chosen:
        source_kind = "transcript"
    elif row.get("source_kind") == "transcript_only" and not chosen:
        source_kind = "not_detected"
    return RecoveredSession(
        session_id=str(row.get("session_id") or row.get("id") or ""),
        title=str(row.get("title") or row.get("session_title") or ""),
        cwd=str((chosen or {}).get("cwd") or row.get("cwd") or row.get("project_uri") or ""),
        project_name=str(row.get("project_name") or ""),
        created=float(row.get("created") or 0),
        updated=float(row.get("updated") or 0),
        session_type=str(row.get("session_type") or ""),
        mode=str(row.get("mode") or ""),
        index_kind=str(row.get("index_kind") or ""),
        source_kind=source_kind,
        selected_path=Path(chosen["path"]) if chosen else None,
        messages=messages,
        candidate_count=len(candidates),
        candidates=candidates,
        metadata=row,
    )


def recover_all(
    product: str,
    *,
    configured_index: Path | None = None,
    compact_root: Path | None = None,
    full_root: Path | None = None,
    extra_roots: Iterable[Path] | None = None,
    include_messages: bool = True,
    include_preview: bool = False,
    preview_chars: int = 500,
    max_files: int = 20000,
    use_default_indexes: bool = True,
) -> tuple[list[RecoveredSession], list[str]]:
    sessions, warnings = collect_index_sessions(
        product,
        configured_index=configured_index,
        use_default_indexes=use_default_indexes,
    )
    index = build_transcript_index(
        resolve_transcript_roots(
            product,
            compact_root=compact_root,
            full_root=full_root,
            extra_roots=extra_roots,
        ),
        max_files=max_files,
    )
    recovered = [
        _session_from_row(
            row,
            evaluate_candidates(
                str(row.get("session_id") or row.get("id") or ""),
                index,
                include_messages=include_messages,
                include_preview=include_preview,
                preview_chars=preview_chars,
            ),
            include_messages=include_messages,
        )
        for row in sessions
    ]
    return recovered, warnings


def recover_query(
    product: str,
    *,
    query: str | None = None,
    session_id: str | None = None,
    configured_index: Path | None = None,
    compact_root: Path | None = None,
    full_root: Path | None = None,
    extra_roots: Iterable[Path] | None = None,
    include_preview: bool = False,
    preview_chars: int = 500,
    limit: int = 20,
    max_files: int = 20000,
) -> dict[str, Any]:
    sessions, warnings = collect_index_sessions(
        product,
        configured_index=configured_index,
        use_default_indexes=configured_index is None,
    )
    selected = select_sessions(sessions, session_id=session_id, query=query, limit=max(1, limit))
    if session_id and not selected:
        selected = [
            {
                "id": session_id,
                "session_id": session_id,
                "source_kind": "transcript_only",
                "index_kind": "",
            }
        ]
        warnings.append(
            "Exact session id was not present in chat_session; trying plaintext roots directly."
        )

    roots = resolve_transcript_roots(
        product,
        compact_root=compact_root,
        full_root=full_root,
        extra_roots=extra_roots,
    )
    index = build_transcript_index(roots, max_files=max(1, max_files))
    recovered = [
        _session_from_row(
            row,
            evaluate_candidates(
                str(row.get("session_id") or row.get("id") or ""),
                index,
                include_messages=include_preview,
                include_preview=include_preview,
                preview_chars=max(50, preview_chars),
            ),
            include_messages=include_preview,
        )
        for row in selected
    ]

    payload_sessions = []
    for item in recovered:
        chosen = item.candidates[0] if item.candidates else None
        if chosen and "messages" in chosen:
            chosen = dict(chosen)
            chosen.pop("messages", None)
        payload_sessions.append(
            {
                "metadata": item.metadata,
                "source_kind": item.source_kind,
                "candidate_count": item.candidate_count,
                "selected_transcript": chosen,
                "candidates": [
                    {key: value for key, value in candidate.items() if key != "messages"}
                    for candidate in item.candidates
                ],
            }
        )

    product_layout = layout(product)
    paths = {
        "index_db": str((configured_index or product_layout.index_dbs[0]).resolve(strict=False)),
        "compact_root": str((compact_root or product_layout.transcript_roots[0][0]).resolve(strict=False)),
        "full_root": str((full_root or product_layout.transcript_roots[1][0]).resolve(strict=False)),
    }
    extra_full = [str(path.resolve(strict=False)) for path, kind in product_layout.transcript_roots[2:] if kind == "full_transcript"]
    if extra_full:
        paths["additional_full_roots"] = extra_full

    return {
        "schema": "find-agent-data/qoder-map-v1",
        "product": product,
        "read_only": True,
        "selection_rule": SELECTION_RULE,
        "paths": paths,
        "matched_sessions": len(payload_sessions),
        "sessions": payload_sessions,
        "warnings": warnings,
    }


def select_sessions(
    sessions: list[dict[str, Any]],
    *,
    session_id: str | None,
    query: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if session_id:
        key = session_id.casefold()
        return [
            row
            for row in sessions
            if str(row.get("session_id") or row.get("id") or "").casefold() == key
        ][:limit]
    if query:
        key = query.casefold()
        return [
            row
            for row in sessions
            if key in str(row.get("session_title") or row.get("title") or "").casefold()
            or key in str(row.get("session_id") or row.get("id") or "").casefold()
            or key in str(row.get("project_name") or "").casefold()
        ][:limit]
    return sessions[:limit]
