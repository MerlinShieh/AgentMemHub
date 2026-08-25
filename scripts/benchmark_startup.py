"""Measure listener readiness, index readiness, and first-list latency."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_REPO = Path(__file__).resolve().parent.parent


def get_json(url: str, timeout: float = 2) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8793
    repo = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else DEFAULT_REPO
    env = dict(os.environ)
    if len(sys.argv) > 3:
        env["CONVERSATION_HUB_DATA_DIR"] = str(Path(sys.argv[3]).resolve())
    base = f"http://127.0.0.1:{port}"
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "server.py", "--no-open", "--port", str(port)],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener_ms = None
    index_ready_ms = None
    health: dict = {}
    try:
        deadline = started + 120
        while time.perf_counter() < deadline:
            try:
                health = get_json(base + "/api/health", timeout=1)
                if listener_ms is None:
                    listener_ms = (time.perf_counter() - started) * 1000
                if "index" not in health:
                    # v0.1.x built its initial index before binding the listener.
                    health["index"] = {
                        "status": "legacy-ready",
                        "ready": True,
                        "conversations": None,
                    }
                    index_ready_ms = (time.perf_counter() - started) * 1000
                    break
                if health.get("index", {}).get("status") in {"ready", "error"}:
                    index_ready_ms = (time.perf_counter() - started) * 1000
                    break
            except Exception:
                pass
            time.sleep(0.025)

        list_started = time.perf_counter()
        query = urllib.parse.urlencode({
            "source": "all",
            "q": "",
            "range": "all",
            "status": "all",
            "workspace": "all",
            "native_project": "all",
            "favorites": "0",
            "tag": "",
            "limit": "120",
            "offset": "0",
        })
        conversations = get_json(base + "/api/conversations?" + query, timeout=30)
        first_list_ms = (time.perf_counter() - list_started) * 1000
        search_settled_ms = None
        search_meta_count = None
        data_dir = Path(env.get("CONVERSATION_HUB_DATA_DIR") or repo)
        notes_db = data_dir / "hub_notes.sqlite"
        settle_deadline = time.perf_counter() + 120
        while notes_db.is_file() and time.perf_counter() < settle_deadline:
            try:
                with sqlite3.connect(notes_db, timeout=1) as conn:
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "conversation_search_meta" not in tables:
                        break
                    search_meta_count = int(
                        conn.execute("SELECT count(*) FROM conversation_search_meta").fetchone()[0]
                    )
                    if search_meta_count >= int(conversations.get("total") or 0):
                        search_settled_ms = (time.perf_counter() - started) * 1000
                        break
            except sqlite3.DatabaseError:
                pass
            time.sleep(0.1)
        payload = {
            "listener_ms": round(listener_ms or 0, 1),
            "index_ready_ms": round(index_ready_ms or 0, 1),
            "index": health.get("index", {}),
            "first_list_ms": round(first_list_ms, 1),
            "returned": len(conversations.get("items", [])),
            "total": conversations.get("total", 0),
            "search_index_settle_ms": (
                round(search_settled_ms, 1) if search_settled_ms is not None else None
            ),
            "search_meta_count": search_meta_count,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload["index"].get("status") in {"ready", "legacy-ready"} else 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
