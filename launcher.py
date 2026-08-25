from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from app_paths import CONFIG_PATH, DATA_DIR


APP_DIR = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765/"
REQUIRED_APP_VERSION = "0.4.1"


def process_options(*, detached: bool = False) -> dict:
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached:
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags}
    return {"start_new_session": detached}


def get_json(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(URL.rstrip("/") + path, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def python_executable() -> str:
    if os.name == "nt":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def ensure_grok_enabled() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    except (OSError, ValueError, TypeError):
        payload = {}
    extra = payload.get("extra_sources")
    extra = extra if isinstance(extra, dict) else {}
    grok = extra.get("grok")
    grok = grok if isinstance(grok, dict) else {}
    extra["grok"] = {
        "enabled": True,
        "path": grok.get("path") or str(Path.home() / ".grok"),
    }
    payload["extra_sources"] = extra
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def running_server_is_current() -> bool:
    health = get_json("/api/health")
    if not health or str(health.get("app_version") or "") < REQUIRED_APP_VERSION:
        return False
    setup = get_json("/api/setup/status") or {}
    grok = ((setup.get("sources") or {}).get("grok") or {})
    return bool(grok.get("enabled") and grok.get("valid"))


def post_json(path: str, body: dict) -> dict | None:
    token_data = get_json("/api/token")
    if not token_data:
        return None
    request = urllib.request.Request(
        URL.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Token": str(token_data["token"]),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def stop_stale_hub_processes() -> None:
    if os.name == "nt":
        _stop_stale_hub_processes_windows()
        return
    if sys.platform == "darwin":
        _stop_stale_hub_processes_posix()


def _stop_stale_hub_processes_windows() -> None:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine } | "
            "Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **process_options(),
    )
    raw = (completed.stdout or "").strip()
    if not raw:
        return
    try:
        rows = json.loads(raw)
    except ValueError:
        return
    if isinstance(rows, dict):
        rows = [rows]
    markers = (
        "aiconversationhub.exe",
        r"\aiconversationhub\server.py",
        r"\aiconversationhub\desktop_app.py",
        r"\programs\aiconversationhub",
        r"\desktop\aiconversationhub",
        r"\ai-conversation-hub\server.py",
        r"\ai-conversation-hub\desktop_app.py",
    )
    self_pid = os.getpid()
    for row in rows:
        pid = int(row.get("ProcessId") or 0)
        command = str(row.get("CommandLine") or "").casefold()
        name = str(row.get("Name") or "").casefold()
        if pid in {0, self_pid} or "launcher.py" in command:
            continue
        if name == "aiconversationhub.exe" or any(marker in command for marker in markers):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, **process_options())
    time.sleep(0.6)


def _stop_stale_hub_processes_posix() -> None:
    completed = subprocess.run(
        ["pgrep", "-fl", "AIConversationHub|ai-conversation-hub|server.py"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    self_pid = os.getpid()
    markers = ("aiconversationhub", "ai-conversation-hub", "conversation_hub")
    for line in (completed.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1].casefold()
        if pid in {0, self_pid} or "launcher.py" in command:
            continue
        if any(marker in command for marker in markers):
            try:
                os.kill(pid, 15)
            except OSError:
                continue
    time.sleep(0.6)


def start_current_server() -> None:
    subprocess.Popen(
        [python_executable(), str(APP_DIR / "server.py"), "--no-open"],
        cwd=str(APP_DIR),
        **process_options(detached=True),
    )
    for _ in range(60):
        if get_json("/api/health"):
            return
        time.sleep(0.25)


def main() -> None:
    os.chdir(APP_DIR)
    ensure_grok_enabled()
    if not running_server_is_current():
        stop_stale_hub_processes()
        start_current_server()
        post_json("/api/sources/enabled", {"source": "grok", "enabled": True})
        post_json("/api/refresh", {})
    else:
        post_json("/api/sources/enabled", {"source": "grok", "enabled": True})
        post_json("/api/refresh", {})
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
