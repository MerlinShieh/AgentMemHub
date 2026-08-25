"""OpenCode Adapter。

数据位置：~/.local/share/opencode/opencode.db（Windows 下即 %USERPROFILE%\.local\share\opencode\opencode.db）
复用通用 SQLite 会话 Adapter（session/message/part 结构）。
"""
from __future__ import annotations

import os
from pathlib import Path

from .sqlite_conversation import SqliteConversationAdapter


class OpenCodeAdapter(SqliteConversationAdapter):
    source = "opencode"
    label = "OpenCode"

    def candidate_paths(self) -> list[Path]:
        env = os.environ.get("OPENCODE_HOME", "").strip()
        paths: list[Path] = []
        if env:
            paths.append(Path(env) / "opencode.db")
            paths.append(Path(env))
        # XDG 风格 data dir
        home = Path.home()
        paths.append(home / ".local" / "share" / "opencode" / "opencode.db")
        paths.append(home / ".local" / "share" / "opencode")
        return paths
