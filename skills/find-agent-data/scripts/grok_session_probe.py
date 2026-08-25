#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI wrapper around the shared Grok Build recovery layer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from agent_recovery.grok import recover_query  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map Grok Build session metadata to the best plaintext updates.jsonl."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--query", help="Case-insensitive title, session-id, or cwd substring")
    group.add_argument("--session-id", help="Exact session id")
    parser.add_argument("--home", type=Path, help="Override GROK_HOME")
    parser.add_argument("--preview", action="store_true", help="Include short redacted goal/latest-message previews")
    parser.add_argument("--preview-chars", type=int, default=500)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of concise text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = recover_query(
        query=args.query,
        session_id=args.session_id,
        home=args.home,
        include_preview=args.preview,
        preview_chars=args.preview_chars,
        limit=args.limit,
    )
    if args.json:
        stdout_encoding = str(getattr(sys.stdout, "encoding", "") or "").casefold()
        print(json.dumps(payload, ensure_ascii="utf" not in stdout_encoding, indent=2, default=str))
    else:
        print(f"grok: {payload['matched_sessions']} session(s), read-only")
        for item in payload["sessions"]:
            metadata = item["metadata"]
            print(f"\n{metadata.get('session_title') or '(untitled)'}")
            print(f"  id: {metadata.get('session_id')}")
            print(f"  source: {item['source_kind']}; cwd: {metadata.get('cwd')}")
            chosen = item["selected_transcript"]
            if chosen and chosen.get("message_count"):
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
                print("  limitation: summary exists, but updates.jsonl had no user/assistant text")
        for warning in payload["warnings"]:
            print(f"\nWARNING: {warning}")
    return 0 if payload["sessions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
