#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI wrapper around the shared Qoder recovery layer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from agent_recovery.qoder import known_products, recover_query  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map Qoder/QoderCN session metadata to the best plaintext transcript."
    )
    parser.add_argument("--product", choices=known_products(), default="qoder")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--query", help="Case-insensitive title, session-id, or project substring")
    group.add_argument("--session-id", help="Exact session id")
    parser.add_argument("--index-db", type=Path, help="Override chat_session index database")
    parser.add_argument("--compact-root", type=Path, help="Override compact transcript root")
    parser.add_argument("--full-root", type=Path, help="Override full transcript root")
    parser.add_argument("--preview", action="store_true", help="Include short redacted goal/latest-message previews")
    parser.add_argument("--preview-chars", type=int, default=500)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-files", type=int, default=20000, help="Per-root safety bound")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of concise text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = recover_query(
        args.product,
        query=args.query,
        session_id=args.session_id,
        configured_index=args.index_db,
        compact_root=args.compact_root,
        full_root=args.full_root,
        include_preview=args.preview,
        preview_chars=args.preview_chars,
        limit=args.limit,
        max_files=args.max_files,
    )
    if args.json:
        stdout_encoding = str(getattr(sys.stdout, "encoding", "") or "").casefold()
        print(json.dumps(payload, ensure_ascii="utf" not in stdout_encoding, indent=2, default=str))
    else:
        print(f"{args.product}: {payload['matched_sessions']} session(s), read-only")
        for item in payload["sessions"]:
            metadata = item["metadata"]
            print(f"\n{metadata.get('session_title') or metadata.get('title') or '(untitled)'}")
            print(f"  id: {metadata.get('session_id')}")
            print(f"  source: {item['source_kind']}; candidates: {item['candidate_count']}")
            chosen = item["selected_transcript"]
            if chosen:
                print(f"  selected: {chosen['path']}")
                print(
                    f"  coverage: {chosen['message_count']} messages "
                    f"({chosen['user_messages']} user / {chosen['assistant_messages']} assistant)"
                )
                preview = chosen.get("preview")
                if preview:
                    for key in ("original_goal", "latest_user", "latest_assistant_after_user"):
                        value = preview.get(key)
                        if value:
                            print(f"  {key}: {value['text']}")
            else:
                print("  limitation: metadata was found, but no matching plaintext transcript was found")
        for warning in payload["warnings"]:
            print(f"\nWARNING: {warning}")
    return 0 if payload["sessions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
