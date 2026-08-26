-- AgentMemHub 统一存储 schema
-- 会话元数据 + 全量事件流 + FTS5 全文搜索

-- 会话元数据（保持与 ai-conversation-hub 兼容的 source 感知列）
CREATE TABLE IF NOT EXISTS conversations (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    title        TEXT,
    cwd          TEXT,
    model        TEXT,
    created_at   INTEGER,
    updated_at   INTEGER,
    event_count  INTEGER DEFAULT 0,
    roles_json   TEXT,               -- ["user","tool","reasoning",...]
    meta_json    TEXT,               -- 原始元数据保底
    signature    TEXT,               -- 源文件指纹（增量重建依据）
    PRIMARY KEY (source, id)
);

-- 全量事件流（会话内按 seq 有序）
CREATE TABLE IF NOT EXISTS events (
    source          TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,   -- user|assistant|tool|reasoning|patch|shell|meta
    content         TEXT,
    tool_name       TEXT,
    tool_input_json TEXT,
    tool_output     TEXT,
    tool_status     TEXT,
    reasoning       TEXT,
    patch_file      TEXT,
    patch_diff      TEXT,
    shell_cmd       TEXT,
    shell_output    TEXT,
    shell_cwd       TEXT,
    parent_id       TEXT,
    time            INTEGER,
    model           TEXT,
    raw_json        TEXT,            -- 原始事件 JSON 无损保底
    PRIMARY KEY (source, conversation_id, seq)
);

-- 事件正文 FTS5（中文友好 + 短语）
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    source UNINDEXED,
    conversation_id UNINDEXED,
    role,
    content,
    tool_name,
    tool_output,
    reasoning,
    shell_cmd,
    shell_output,
    patch_diff,
    tokenize = 'unicode61 remove_diacritics 2'
);