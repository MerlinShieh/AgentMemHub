#!/usr/bin/env python3
"""Discover local sources and install Conversation Hub access for local agents."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parents[1]
if str(HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(HUB_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="Override home directory")
    parser.add_argument("--no-discover", action="store_true")
    parser.add_argument("--no-mcp", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    from agent_setup import run_setup

    result = run_setup(
        home=Path(args.home).expanduser() if args.home else None,
        discover_sources=not args.no_discover,
        register_mcp=not args.no_mcp,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        print("done. usage guide ->", result["usage_path"])
        print("restart Codex/Grok to reload MCP configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
