"""源库只读访问层。

铁律：agentmemhub.db 是 AgentMemHub 的采集库且有 daemon 活跃写入，
本模块只允许以 mode=ro URI 打开，任何写路径都是 bug。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# tokenizer 侧另有 512 token 截断；这里按字符粗裁，防止 2.6M 级异常值白耗性能
MAX_CHARS = 4096

# 默认向量化角色白名单（reasoning 走开关，tool/patch/meta 永不入库）
DEFAULT_ROLES = ("user", "assistant")


def open_source_ro(db_path: Path | str) -> sqlite3.Connection:
    """以只读 URI 打开源库。写入会抛 sqlite3.OperationalError(readonly)。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"源库不存在：{p}")
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def validate_source_schema(conn: sqlite3.Connection) -> None:
    """确认源库具备预期结构（schema 漂移早失败，别默默嵌出错乱的库）。"""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = {"conversations", "events"} - tables
    if missing:
        raise ValueError(f"源库缺少预期表：{sorted(missing)}")


def prep_text(content: str | None) -> str | None:
    """向量化前文本清洗：去空白、空内容淘汰、超长粗裁。返回 None 表示跳过。"""
    if not content:
        return None
    text = content.strip()
    if not text:
        return None
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    return text
