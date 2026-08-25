#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


EXPECTED_SOURCES = {
    "hermes", "codex", "workbuddy", "claude", "cursor", "qclaw",
    "qoderwork", "zcode", "codepilot", "marvis", "qoder", "qodercn",
    "qwenworkcn", "grok",
}


def mcp_frame(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(payload) + payload


def parse_mcp_frames(data: bytes) -> list[dict]:
    messages: list[dict] = []
    while data:
        header, separator, remainder = data.partition(b"\r\n\r\n")
        if not separator:
            raise AssertionError(f"invalid MCP response header: {data[:200]!r}")
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        if length <= 0 or len(remainder) < length:
            raise AssertionError("invalid MCP content length")
        messages.append(json.loads(remainder[:length].decode("utf-8")))
        data = remainder[length:]
    return messages


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_instance(data_dir: Path, process: subprocess.Popen[bytes]) -> tuple[int, dict]:
    instance = data_dir / "instance.json"
    last_error = "instance.json not written"
    for _ in range(600):
        if process.poll() is not None:
            raise RuntimeError(f"frozen desktop exited early: {process.returncode}")
        try:
            port = int(json.loads(instance.read_text(encoding="utf-8"))["port"])
            health = get_json(f"http://127.0.0.1:{port}/api/health")
            index = health.get("index") or {}
            if index.get("status") == "error":
                raise RuntimeError(f"frozen index failed: {index.get('error')}")
            if index.get("ready"):
                return port, health
            last_error = f"index status: {index.get('status')}"
        except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"frozen desktop startup timeout: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    desktop = Path(args.desktop).resolve()
    agent = Path(args.agent).resolve()
    if not desktop.is_file() or not agent.is_file():
        raise FileNotFoundError(f"missing frozen executable: {desktop} / {agent}")

    with tempfile.TemporaryDirectory(prefix="hub-frozen-smoke-") as directory:
        data_dir = Path(directory) / "data"
        env = dict(os.environ)
        env["CONVERSATION_HUB_DATA_DIR"] = str(data_dir)
        process = subprocess.Popen(
            [str(desktop), "--no-open"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            port, health = wait_for_instance(data_dir, process)
            sources = get_json(f"http://127.0.0.1:{port}/api/sources").get("sources") or {}
            missing = EXPECTED_SOURCES.difference(sources)
            if missing:
                raise AssertionError(f"frozen /api/sources missing: {sorted(missing)}")
            for name in ("qoder", "qodercn", "grok"):
                detail = sources[name]
                error = str(detail.get("error") or "")
                if "agent_recovery" in error or "ModuleNotFoundError" in error:
                    raise AssertionError(f"{name} recovery module missing: {error}")

            ping = subprocess.run(
                [str(agent), "ping"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            payload = json.loads(ping.stdout)
            if not payload.get("ok") or payload.get("app_version") != health.get("app_version"):
                raise AssertionError(f"frozen agent ping mismatch: {payload}")

            setup_home = Path(directory) / "agent-home"
            setup = subprocess.run(
                [str(agent), "setup", "--home", str(setup_home), "--json"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            setup_result = json.loads(setup.stdout)
            if len(setup_result.get("skills") or []) != 6:
                raise AssertionError(f"frozen setup did not install both Skills: {setup_result}")
            if not Path(str(setup_result.get("usage_path") or "")).is_file():
                raise AssertionError("frozen setup did not generate AGENT_USAGE.md")
            for config_name in (".codex/config.toml", ".grok/config.toml"):
                if not (setup_home / config_name).is_file():
                    raise AssertionError(f"frozen setup missing {config_name}")

            mcp_input = b"".join(
                (
                    mcp_frame(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"},
                        }
                    ),
                    mcp_frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                )
            )
            mcp = subprocess.run(
                [str(agent), "mcp"],
                env=env,
                input=mcp_input,
                check=True,
                capture_output=True,
                timeout=60,
            )
            responses = parse_mcp_frames(mcp.stdout)
            tools = ((responses[-1].get("result") or {}).get("tools") or [])
            tool_names = {str(item.get("name") or "") for item in tools}
            if not {"hub_ping", "hub_search", "hub_conversation", "hub_handoff"}.issubset(tool_names):
                raise AssertionError(f"frozen MCP tools missing: {sorted(tool_names)}")
            print(
                "FROZEN PASS:",
                health.get("platform"),
                health.get("app_version"),
                len(sources),
                "sources + agent setup/CLI/MCP",
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
