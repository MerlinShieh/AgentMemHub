#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def run_setup_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Install AI Conversation Hub Agent access")
    parser.add_argument("--home", help="Override home directory (tests/managed installs)")
    parser.add_argument("--no-discover", action="store_true")
    parser.add_argument("--no-mcp", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from agent_setup import run_setup

    result = run_setup(
        home=Path(args.home).expanduser() if args.home else None,
        discover_sources=not args.no_discover,
        register_mcp=not args.no_mcp,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        print("AI Conversation Hub Agent access installed.")
        print("Usage guide:", result["usage_path"])
        print("Restart Codex/Grok to reload MCP configuration.")
    return 0


def run_agent_command(argv: list[str]) -> int:
    from desktop_app import ensure_server_started

    port, _server_thread = ensure_server_started(wait_for_index=True)
    os.environ["CONVERSATION_HUB_PORT"] = str(port)
    import hub_agent

    hub_agent.PORT = str(port)
    hub_agent.BASE = f"http://127.0.0.1:{port}"
    sys.argv = [sys.argv[0], *argv]
    hub_agent.main()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "setup":
        return run_setup_command(argv[1:])
    return run_agent_command(argv)


if __name__ == "__main__":
    raise SystemExit(main())
