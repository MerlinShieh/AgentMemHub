#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover local AI-assistant data by known path rules only.

This command is deliberately read-only. It does not scan an entire disk, read
message bodies, open credential files, or try to decrypt vendor databases.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from agent_recovery.grok import discovery_locations as grok_locations  # noqa: E402
from agent_recovery.qoder import layout as qoder_layout  # noqa: E402


HOME = Path.home()
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
APP_SUPPORT = HOME / "Library" / "Application Support" if IS_MAC else APPDATA


def location(role: str, path: Path, storage: str, *, evidence: bool = True) -> dict[str, Any]:
    return {"role": role, "path": path, "storage": storage, "evidence": evidence}


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
WORKBUDDY_HOME = Path(os.environ.get("WORKBUDDY_HOME") or HOME / ".workbuddy")
OPENCODE_HOME = Path(os.environ.get("OPENCODE_DATA_DIR") or HOME / ".local" / "share" / "opencode")
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes")
HERMES_DB = Path(os.environ.get("CONVERSATION_HUB_HERMES_DB") or HERMES_HOME / "state.db")


# Confidence is about the storage rule, not whether the product is installed.
# verified: inspected locally or confirmed against a stable upstream layout.
# partial: a useful local root is known, but message coverage/schema is incomplete.
AGENTS: dict[str, dict[str, Any]] = {
    "hermes": {
        "label": "Hermes",
        "confidence": "verified",
        "locations": [location("conversation_db", HERMES_DB, "sqlite")],
    },
    "codex": {
        "label": "Codex CLI/Desktop",
        "confidence": "verified",
        "locations": [
            location("session_index", CODEX_HOME / "state_5.sqlite", "sqlite"),
            location("transcript_root", CODEX_HOME / "sessions", "jsonl"),
            location("archived_transcript_root", CODEX_HOME / "archived_sessions", "jsonl"),
        ],
    },
    "workbuddy": {
        "label": "WorkBuddy",
        "confidence": "verified",
        "locations": [
            location("conversation_db", WORKBUDDY_HOME / "workbuddy.db", "sqlite"),
            location("project_root", WORKBUDDY_HOME / "projects", "mixed"),
        ],
    },
    "claude": {
        "label": "Claude Code",
        "confidence": "verified",
        "locations": [
            location("session_index", HOME / ".claude" / "history.jsonl", "jsonl"),
            location("transcript_root", HOME / ".claude" / "projects", "jsonl"),
        ],
    },
    "qoderwork": {
        "label": "QoderWork / QwenWork desktop",
        "confidence": "verified",
        "locations": [
            location("conversation_db", APP_SUPPORT / name / "data" / "agents.db", "sqlite")
            for name in ("QoderWork CN", "QoderWork", "QwenWorkCN", "QwenWork")
        ],
    },
    "zcode": {
        "label": "ZCode",
        "confidence": "verified",
        "locations": [location("conversation_db", HOME / ".zcode" / "cli" / "db" / "db.sqlite", "sqlite")],
    },
    "opencode": {
        "label": "OpenCode",
        "confidence": "verified",
        "locations": [
            location("data_root", OPENCODE_HOME, "mixed", evidence=False),
            location("session_root", OPENCODE_HOME / "storage" / "session", "json"),
            location("message_root", OPENCODE_HOME / "storage" / "message", "json"),
            location("conversation_db", OPENCODE_HOME / "opencode.db", "sqlite"),
        ],
        "warnings": ["The data root can contain auth.json. This tool never reads or exports it."],
    },
    "gemini": {
        "label": "Gemini CLI",
        "confidence": "verified",
        "locations": [location("transcript_root", HOME / ".gemini" / "tmp", "json")],
    },
    "trae": {
        "label": "Trae / Trae CN",
        "confidence": "partial",
        "locations": [
            location("workspace_root", APPDATA / "Trae CN" / "User" / "workspaceStorage", "vscode-sqlite"),
            location("workspace_root", APPDATA / "Trae" / "User" / "workspaceStorage", "vscode-sqlite"),
        ],
    },
    "qoder": {
        "label": "Qoder international",
        "confidence": "verified",
        "locations": qoder_layout("qoder").discovery_locations(),
        "warnings": ["chat_message.content is encrypted. Use chat_session metadata and plaintext transcripts; do not attempt decryption."],
    },
    "qodercn": {
        "label": "QoderCN",
        "confidence": "verified",
        "locations": qoder_layout("qodercn").discovery_locations(),
        "warnings": ["The home directory is .qoder-cn (with a hyphen). Never attempt to decrypt chat_message.content."],
    },
    "qwenworkcn": {
        "label": "QwenWorkCN CLI",
        "confidence": "verified",
        "locations": [
            location("conversation_root", HOME / ".qwenworkcn" / "workspace", "json"),
            location("session_index", HOME / ".qwenworkcn" / "awareness" / "main" / ".index.sqlite", "sqlite"),
        ],
    },
    "qclaw": {
        "label": "QClaw",
        "confidence": "partial",
        "locations": [
            location("audit_db", APPDATA / "QClaw" / "qclaw.db", "sqlite"),
            location("conversation_store", APPDATA / "QClaw" / "IndexedDB" / "file__0.indexeddb.leveldb", "leveldb"),
        ],
    },
    "marvis": {
        "label": "Tencent Marvis",
        "confidence": "partial",
        "locations": [
            location("application_db_root", APPDATA / "Tencent" / "Marvis" / "db", "mixed", evidence=False),
            location("cef_store", APPDATA / "Tencent" / "Marvis" / "cef" / "CEF_Marvis", "leveldb"),
        ],
        "warnings": ["Presence of the application db directory alone is not evidence of recoverable conversations."],
    },
    "codebuddy": {
        "label": "CodeBuddy",
        "confidence": "partial",
        "locations": [
            location("ide_workspace_root", APPDATA / "CodeBuddy" / "User" / "workspaceStorage", "vscode-sqlite"),
            location("cli_root", HOME / ".codebuddycn" / "projects", "jsonl"),
            location("legacy_extension_root", LOCALAPPDATA / "CodeBuddyExtension", "mixed", evidence=False),
        ],
    },
    "lobsterai": {
        "label": "LobsterAI",
        "confidence": "verified",
        "locations": [
            location("conversation_db", APPDATA / "LobsterAI" / "lobsterai.sqlite", "sqlite"),
            location("runtime_root", APPDATA / "LobsterAI" / "openclaw", "mixed", evidence=False),
        ],
        "warnings": ["The runtime root may contain secrets. Do not export it as a conversation archive."],
    },
    "autoclaw": {
        "label": "AutoClaw",
        "confidence": "partial",
        "locations": [location("workspace_root", HOME / ".openclaw-autoclaw" / "workspace", "json")],
    },
    "grok": {
        "label": "Grok Build",
        "confidence": "verified",
        "locations": grok_locations(),
        "warnings": [
            "Prefer summary.json + updates.jsonl. Never read auth.json. session_search.sqlite is a derived search index, not the transcript source of truth.",
        ],
    },
    "dumate": {
        "label": "DuMate",
        "confidence": "verified",
        "locations": [
            location("workspace_db_root", APPDATA / "qianfan-desktop-app" / "qianfan_desk_xdg", "sqlite"),
            location("openclaw_state", HOME / ".openclaw" / "state" / "openclaw.sqlite", "sqlite", evidence=False),
            location("browser_log_root", HOME / ".qianfan" / "workspace", "json", evidence=False),
        ],
        "warnings": ["openclaw.json and adjacent runtime configuration may contain keys. Never read or export them."],
    },
}

ALIASES = {
    "claude-code": "claude",
    "code-buddy": "codebuddy",
    "gemini-cli": "gemini",
    "lobster-ai": "lobsterai",
    "open-code": "opencode",
    "qoder-cn": "qodercn",
    "qoder-work": "qoderwork",
    "qwen-work-cn": "qwenworkcn",
    "grok-build": "grok",
    "grok-cli": "grok",
}


def probe_sqlite(path: Path) -> dict[str, Any]:
    """Return schema-only SQLite evidence without reading rows."""
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 100"
                )
            ]
        finally:
            conn.close()
        return {"status": "readable", "tables": tables}
    except Exception as exc:  # Locked, encrypted, non-SQLite, or unsupported URI.
        return {"status": "unreadable", "error": f"{type(exc).__name__}: {exc}"}


def probe_path(path: Path, storage: str) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing"}
    stat = path.stat()
    result: dict[str, Any] = {
        "status": "exists",
        "type": "directory" if path.is_dir() else "file",
        "modified_ns": stat.st_mtime_ns,
    }
    if path.is_file():
        result["bytes"] = stat.st_size
        if storage in {"sqlite", "vscode-sqlite"}:
            result["sqlite"] = probe_sqlite(path)
    return result


def collect_agent(agent_id: str, *, probe: bool = False, existing_only: bool = False) -> dict[str, Any]:
    info = AGENTS[agent_id]
    resolved = []
    evidence_hits = 0
    for spec in info["locations"]:
        path = Path(spec["path"]).expanduser()
        exists = path.exists()
        if exists and spec["evidence"]:
            evidence_hits += 1
        if existing_only and not exists:
            continue
        item = {
            "role": spec["role"],
            "path": str(path.resolve(strict=False)),
            "storage": spec["storage"],
            "exists": exists,
            "conversation_evidence": spec["evidence"],
        }
        if probe:
            item["probe"] = probe_path(path, spec["storage"])
        resolved.append(item)
    return {
        "id": agent_id,
        "label": info["label"],
        "detected": evidence_hits > 0,
        "confidence": info["confidence"],
        "locations": resolved,
        "warnings": list(info.get("warnings", [])),
    }


def print_agent(result: dict[str, Any]) -> None:
    detected = "DETECTED" if result["detected"] else "not detected"
    print(f"### {result['label']} [{result['confidence']}; {detected}]")
    for item in result["locations"]:
        mark = "OK" if item["exists"] else "--"
        qualifier = "" if item["conversation_evidence"] else " (context only)"
        print(f"  {mark} {item['role']}: {item['path']} [{item['storage']}]{qualifier}")
        probe = item.get("probe", {})
        sqlite_probe = probe.get("sqlite") if isinstance(probe, dict) else None
        if sqlite_probe:
            tables = ", ".join(sqlite_probe.get("tables", []))
            print(f"     sqlite={sqlite_probe['status']} tables={tables or '(none)'}")
    for warning in result["warnings"]:
        print(f"  WARNING: {warning}")
    print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", nargs="?", help="Agent id or alias; omit to inspect all known agents")
    parser.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    parser.add_argument("--probe", action="store_true", help="Probe file type and SQLite schema, never message rows")
    parser.add_argument("--existing-only", action="store_true", help="Omit missing locations and undetected agents")
    parser.add_argument("--list", action="store_true", help="List supported agent ids and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        print("\n".join(AGENTS))
        return 0

    targets: list[str]
    if args.agent:
        requested = args.agent.casefold().strip()
        target = ALIASES.get(requested, requested)
        if target not in AGENTS:
            print(f"Unknown agent: {args.agent}. Supported: {', '.join(AGENTS)}", file=sys.stderr)
            return 2
        targets = [target]
    else:
        targets = list(AGENTS)

    results = [collect_agent(key, probe=args.probe, existing_only=args.existing_only) for key in targets]
    if args.existing_only and not args.agent:
        results = [item for item in results if item["detected"]]

    if args.json:
        payload = {
            "schema": "find-agent-data/v2",
            "platform": "windows" if IS_WINDOWS else "macos" if IS_MAC else "linux",
            "read_only": True,
            "agents": results,
        }
        stdout_encoding = str(getattr(sys.stdout, "encoding", "") or "").casefold()
        print(json.dumps(payload, ensure_ascii="utf" not in stdout_encoding, indent=2))
    else:
        print(f"AI agent data discovery ({len(results)} result(s), known paths only, read-only)\n")
        for result in results:
            print_agent(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
