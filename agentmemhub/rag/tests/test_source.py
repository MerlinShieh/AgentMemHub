"""源库访问层测试：只读铁律 + schema 校验 + 文本清洗边界。"""
from __future__ import annotations

import sqlite3

import pytest

from asrag.source import (
    MAX_CHARS,
    open_source_ro,
    prep_text,
    validate_source_schema,
)


def test_prep_text_boundaries():
    assert prep_text(None) is None
    assert prep_text("") is None
    assert prep_text("   \n\t ") is None
    assert prep_text("  有效文本  ") == "有效文本"
    long = prep_text("字" * (MAX_CHARS + 5000))
    assert long is not None and len(long) == MAX_CHARS
    assert prep_text("正常长度消息") == "正常长度消息"


def test_source_open_is_readonly(fixture_source_db):
    """铁律：源库连接必须只读。写入抛错 = 保护生效。"""
    conn = open_source_ro(fixture_source_db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO events(source,conversation_id,seq,role)"
                         " VALUES('x','y',9,'user')")
    finally:
        conn.close()


def test_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_source_ro(tmp_path / "nope.db")


def test_schema_validation_rejects_foreign_db(tmp_path):
    p = tmp_path / "other.db"
    sqlite3.connect(str(p)).execute("CREATE TABLE foo(x)").connection.close()
    conn = open_source_ro(p)
    try:
        with pytest.raises(ValueError, match="缺少预期表"):
            validate_source_schema(conn)
    finally:
        conn.close()
