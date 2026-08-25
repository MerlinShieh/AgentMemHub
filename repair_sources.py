from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from app_paths import CONFIG_PATH, DATA_DIR, RESOURCE_DIR
from source_adapters import (
    configured_custom_sources,
    configured_extra_sources,
    discover_extra_sources,
)


SKIP_DIRS = {
    ".git", ".svn", "__pycache__", "node_modules", "venv", ".venv",
    "runtime", "runtimes", "cache", "caches", "backup", "backups",
    "windowsapps", "$recycle.bin", "system volume information",
}


def application_support() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def atomic_write_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="sources-", suffix=".json.tmp", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, CONFIG_PATH)
    finally:
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            pass


def sqlite_tables(path: Path) -> set[str]:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            try:
                return {row[0] for row in cursor.fetchall()}
            finally:
                cursor.close()
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError):
        return set()


def session_count(path: Path, table: str = "sessions") -> int:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            cursor = conn.execute(f'SELECT count(*) FROM "{table}"')
            try:
                return int(cursor.fetchone()[0])
            finally:
                cursor.close()
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError):
        return 0


def valid_hermes(path: Path) -> bool:
    return path.is_file() and {"sessions", "messages"}.issubset(sqlite_tables(path))


def valid_codex(path: Path) -> bool:
    return path.is_file() and "threads" in sqlite_tables(path)


def valid_workbuddy_home(path: Path) -> bool:
    db = path / "workbuddy.db"
    return (path / "projects").is_dir() and db.is_file() and {
        "sessions", "session_usage", "workspaces",
    }.issubset(sqlite_tables(db))


def source_status(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    data = config or load_config()
    hermes = Path(str(data.get("hermes_db") or "")).expanduser()
    codex = Path(str(data.get("codex_db") or "")).expanduser()
    workbuddy = Path(str(data.get("workbuddy_home") or "")).expanduser()
    result = {
        "hermes": {
            "path": str(hermes) if str(data.get("hermes_db") or "") else "",
            "valid": valid_hermes(hermes),
            "conversations": session_count(hermes),
        },
        "codex": {
            "path": str(codex) if str(data.get("codex_db") or "") else "",
            "valid": valid_codex(codex),
            "conversations": session_count(codex, "threads"),
        },
        "workbuddy": {
            "path": str(workbuddy) if str(data.get("workbuddy_home") or "") else "",
            "valid": valid_workbuddy_home(workbuddy),
            "conversations": session_count(workbuddy / "workbuddy.db"),
        },
    }
    for source, item in configured_extra_sources(data).items():
        result[source] = {
            "path": item["path"],
            "valid": item["valid"],
            "enabled": item["enabled"],
            "detail": item["detail"],
            "conversations": item["conversations"],
        }
    for source, item in configured_custom_sources(data).items():
        result[source] = {
            "path": item["path"],
            "valid": item["valid"],
            "enabled": item["enabled"],
            "detail": item["detail"],
            "conversations": item["conversations"],
            "label": item["label"],
            "format": item["format"],
            "custom": True,
        }
    return result


def walk_named(roots: list[Path], filename: str):
    seen: set[str] = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in SKIP_DIRS and not name.startswith("$")
            ]
            if filename not in files:
                continue
            path = Path(current) / filename
            key = str(path).casefold()
            if key not in seen:
                seen.add(key)
                yield path


def best_database(
    current: str | None,
    defaults: list[Path],
    roots: list[Path],
    filename: str,
    validator: Callable[[Path], bool],
    table: str,
) -> Path | None:
    direct = ([Path(current).expanduser()] if current else []) + defaults
    for path in direct:
        if validator(path):
            return path.resolve()
    candidates = [path for path in walk_named(roots, filename) if validator(path)]
    return max(candidates, key=lambda path: (session_count(path, table), path.stat().st_mtime)).resolve() if candidates else None


def best_workbuddy_home(current: str | None, roots: list[Path]) -> Path | None:
    workbuddy_env = os.environ.get("WORKBUDDY_HOME", "").strip()
    direct = ([Path(current).expanduser()] if current else []) + (
        [Path(workbuddy_env).expanduser()] if workbuddy_env else []
    ) + [
        Path.home() / ".workbuddy",
        application_support() / "WorkBuddy",
        application_support() / "workbuddy",
    ]
    for path in direct:
        if valid_workbuddy_home(path):
            return path.resolve()
    candidates = [path.parent for path in walk_named(roots, "workbuddy.db") if valid_workbuddy_home(path.parent)]
    return max(
        candidates,
        key=lambda path: (session_count(path / "workbuddy.db"), (path / "workbuddy.db").stat().st_mtime),
    ).resolve() if candidates else None


def discovery_roots(extra_roots: list[str] | None = None) -> list[Path]:
    # Standard locations are checked directly above. Recursive discovery is
    # intentionally limited to explicit roots so first run never crawls an
    # entire user profile or backup tree unexpectedly.
    values = [Path(value).expanduser() for value in (extra_roots or []) if value]
    result: list[Path] = []
    seen: set[str] = set()
    for path in values:
        if not str(path) or not path.exists():
            continue
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path.resolve())
    return result


def repair(extra_roots: list[str] | None = None, *, apply: bool = True) -> dict[str, Any]:
    config = load_config()
    roots = discovery_roots(extra_roots)
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    hermes_defaults = ([Path(hermes_home).expanduser() / "state.db"] if hermes_home else []) + [
        Path.home() / ".hermes" / "state.db",
        application_support() / "Hermes" / "state.db",
    ]
    hermes = best_database(
        str(config.get("hermes_db") or "") or None,
        hermes_defaults,
        roots, "state.db", valid_hermes, "sessions",
    )
    codex = best_database(
        str(config.get("codex_db") or "") or None,
        [codex_home / "state_5.sqlite"],
        roots, "state_5.sqlite", valid_codex, "threads",
    )
    workbuddy = best_workbuddy_home(str(config.get("workbuddy_home") or "") or None, roots)

    repaired = dict(config)
    repaired.update({
        "config_version": 4,
        "hermes_db": str(hermes or config.get("hermes_db") or ""),
        "codex_db": str(codex or config.get("codex_db") or ""),
        "workbuddy_home": str(workbuddy or config.get("workbuddy_home") or ""),
    })
    extras = discover_extra_sources(repaired, [str(path) for path in roots])
    repaired["extra_sources"] = {
        source: {
            "enabled": bool(item["enabled"]),
            "path": str(item["path"]),
        }
        for source, item in extras.items()
    }
    if apply:
        atomic_write_config(repaired)
    return repaired


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair AI Conversation Hub source paths")
    parser.add_argument("roots", nargs="*", help="Additional roots to scan")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = repair(args.roots, apply=not args.dry_run)
    if not args.quiet:
        print(f"数据源配置：{CONFIG_PATH}")
        for name, item in source_status(result).items():
            print(f"- {name}: {item['path'] or '未找到'} ({'有效' if item['valid'] else '无效'})")
        print(f"用户数据目录：{DATA_DIR}")


if __name__ == "__main__":
    main()
