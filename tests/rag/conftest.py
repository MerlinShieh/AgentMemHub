"""全局测试夹具：日志目录隔离（绝不写真实 logs/）+ 微型 fixture 源库。"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def tmp_log_dir(tmp_path):
    """注入临时日志目录。任何被测组件的 log 必须走这里。"""
    d = tmp_path / "logs"
    return d


@pytest.fixture(scope="module")
def project_settings():
    """真实项目注册表（只读访问 models.json/models/）。"""
    from agentmemhub.rag.config import load_settings

    return load_settings()


# ── fixture 源库：复刻 agentmemhub.db 关键结构 + 边界数据 ──────────────────

_SOURCE_DDL = """
CREATE TABLE conversations (
    source TEXT NOT NULL, id TEXT NOT NULL, title TEXT, cwd TEXT, model TEXT,
    created_at INTEGER, updated_at INTEGER, event_count INTEGER DEFAULT 0,
    roles_json TEXT, meta_json TEXT, signature TEXT, session_key TEXT,
    PRIMARY KEY (source, id)
);
CREATE TABLE events (
    source TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT, tool_name TEXT, tool_input_json TEXT,
    tool_output TEXT, tool_status TEXT, reasoning TEXT, patch_file TEXT,
    patch_diff TEXT, shell_cmd TEXT, shell_output TEXT, shell_cwd TEXT,
    parent_id TEXT, time INTEGER, model TEXT, raw_json TEXT, src_id TEXT,
    turn_key TEXT, is_system INTEGER DEFAULT 0,
    PRIMARY KEY (source, conversation_id, seq)
);
"""

# (source, conv, seq, role, content, turn_key, src_id, time, title)
_FIXTURE_EVENTS = [
    ("zcode", "conv-a", 1, "user", "bat 批处理为什么报 was unexpected 错误", "1", "msg:1", 1786000001, "批处理调试"),
    ("zcode", "conv-a", 2, "reasoning", "Let me analyze the batch script issue...", "1", "msg:2", 1786000002, "批处理调试"),
    ("zcode", "conv-a", 3, "assistant", "原因是括号出现在代码块内部，需要改写参数形式", "1", "msg:3", 1786000003, "批处理调试"),
    ("zcode", "conv-a", 4, "tool", None, "1", "msg:4", 1786000004, "批处理调试"),
    ("zcode", "conv-a", 5, "user", "   ", "5", "msg:5", 1786000005, "批处理调试"),
    ("zcode", "conv-a", 6, "user", "", "6", "msg:6", 1786000006, "批处理调试"),
    ("hermes", "conv-b", 1, "user", "嵌入模型从 384 维切换到 512 维怎么做", "1", "msg:10", 1786000010, "向量迁移"),
    ("hermes", "conv-b", 2, "assistant", "直写迁移 512 维向量并同步重建偏移契约" + "冗" * 30_000, "1", "msg:11", 1786000011, "向量迁移"),
    ("hermes", "conv-b", 3, "assistant", "全新环境记得重跑模型下载脚本", "1", "msg:12", 1786000012, "向量迁移"),
    ("trae", "conv-c", 1, "meta", "产物清单 node_modules", "1", "msg:20", None, None),
    # 无 conversations 行的孤儿事件（title join 落空也要能吃下）
    ("zcode", "conv-d", 1, "user", "孤儿会话事件没有元数据行", "1", "msg:30", None, None),
    # P0-2 精确标识符通道用例（高熵串，正文唯一持有者）
    ("zcode", "conv-e", 1, "user", "修复 retry_handler_v2_max 这个死字段", "1", "msg:40", None, None),
    # R1：系统注入事件（is_system=1）永不入库——用单独 INSERT 携带该列
]

# 可嵌入单元：role∈{user,assistant} 且内容非空白
# → conv-a:2 + conv-b:3 + conv-d:1 + conv-e:1 = 7
ELIGIBLE_COUNT = 7


@pytest.fixture()
def fixture_source_db(tmp_path):
    """构造微型只读源库（含中文/超长/空白/非白名单角色/孤儿会话等边界）。"""
    p = tmp_path / "fixture_agentmemhub.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_SOURCE_DDL)
    conn.executemany(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, src_id, time) VALUES(?,?,?,?,?,?,?,?)",
        [row[:8] for row in _FIXTURE_EVENTS],
    )
    convs = {
        ("zcode", "conv-a", "批处理调试"),
        ("hermes", "conv-b", "向量迁移"),
        ("trae", "conv-c", None),
    }
    conn.executemany(
        "INSERT INTO conversations(source, id, title) VALUES(?,?,?)",
        sorted(convs),
    )
    # R1 回归：注入型 user 消息（is_system=1）必须被摄取层挡下
    conn.execute(
        "INSERT INTO events(source, conversation_id, seq, role, content,"
        " turn_key, src_id, is_system) VALUES(?,?,?,?,?,?,?,1)",
        ("zcode", "conv-f", 1, "user", "系统注入的伪用户消息不应入库",
         "1", "msg:50"))
    conn.commit()
    conn.close()
    return p
