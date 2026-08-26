"""ZCode Adapter。

数据位置：~/.zcode/cli/db/db.sqlite（与 OpenCode 相同 session/message/part 结构）
复用通用 SQLite 会话 Adapter。
"""
from __future__ import annotations

import os
from pathlib import Path

from .sqlite_conversation import SqliteConversationAdapter


class ZCodeAdapter(SqliteConversationAdapter):
    source = "zcode"
    label = "ZCode"

    def candidate_paths(self) -> list[Path]:
        env = os.environ.get("ZCODE_HOME", "").strip()
        home = Path.home()
        paths: list[Path] = []
        if env:
            paths.append(Path(env) / "cli" / "db" / "db.sqlite")
        for root in (home / ".zcode",):
            p = root / "cli" / "db" / "db.sqlite"
            if p.exists():
                paths.insert(0, p)
            paths.append(p)
        paths.append(home / ".zcode" / "cli" / "db" / "db.sqlite")
        return list(dict.fromkeys(paths))
