# Agent storage registry

This registry records known-path rules, not installation claims. `verified` means the rule has been inspected locally or matches a stable upstream layout. `partial` means the root is useful but full message coverage/schema is not yet established. Treat product updates as potential schema drift.

## Primary registry

| ID | Confidence | Index / database | Plaintext conversation evidence | Notes |
|---|---|---|---|---|
| `codex` | verified | `$CODEX_HOME/state_5.sqlite` | `$CODEX_HOME/sessions/**/*.jsonl`, `archived_sessions/**/*.jsonl` | `CODEX_HOME` defaults to `~/.codex`. |
| `claude` | verified | `~/.claude/history.jsonl` | `~/.claude/projects/**/*.jsonl` | Project folders hold full session JSONL. |
| `grok` | verified | `$GROK_HOME/sessions/session_search.sqlite` (derived) | `$GROK_HOME/sessions/<encoded-cwd>/<session-id>/summary.json` + `updates.jsonl` | `GROK_HOME` defaults to `~/.grok`. Never read `auth.json`. Skip `subagents/` children. |
| `hermes` | verified | `$CONVERSATION_HUB_HERMES_DB` or `$HERMES_HOME/state.db` | SQLite message rows | Prefer the explicit DB override. |
| `workbuddy` | verified | `$WORKBUDDY_HOME/workbuddy.db` | DB plus `projects/` | Defaults to `~/.workbuddy`. |
| `qoder` | verified | `%APPDATA%/Qoder/SharedClientCache/cache/db/local.db`; legacy `state.vscdb` | `~/.qoder/cache/projects/**/conversation-history/**/*.jsonl`, `~/.qoder/projects/**/transcript/*.jsonl` | Use `qoder_session_probe.py`; never decrypt `chat_message.content`. |
| `qodercn` | verified | `%APPDATA%/QoderCN/SharedClientCache/cache/db/local.db`; legacy `state.vscdb` | `~/.qoder-cn/cache/projects/**/conversation-history/**/*.jsonl`; `%APPDATA%/QoderCN/SharedClientCache/cli/projects/**/*.jsonl` | Home directory includes a hyphen. |
| `qoderwork` | verified | `%APPDATA%/{QoderWork CN,QoderWork,QwenWorkCN,QwenWork}/data/agents.db` | SQLite message rows | Four observed directory-name variants. |
| `qwenworkcn` | verified | `~/.qwenworkcn/awareness/main/.index.sqlite` | `~/.qwenworkcn/workspace/*/conversations.json` | Keep desktop and CLI variants distinct. |
| `zcode` | verified | `~/.zcode/cli/db/db.sqlite` | SQLite message rows | Do not confuse with Zed. |
| `opencode` | verified | `$OPENCODE_DATA_DIR/opencode.db` when present | `$OPENCODE_DATA_DIR/storage/{session,message}/` | Data root can contain `auth.json`; exclude it. |
| `gemini` | verified | project/session metadata in JSON | `~/.gemini/tmp/*/chats/session-*.json` | Project-hash directories. |
| `trae` | partial | `%APPDATA%/{Trae CN,Trae}/User/workspaceStorage/*/state.vscdb` | ItemTable entries where supported | Schema/key names vary with IDE version. |
| `codebuddy` | partial | `%APPDATA%/CodeBuddy/User/workspaceStorage/*/state.vscdb` | `~/.codebuddycn/projects/` for CLI/fork format | Standalone IDE, plugin and CLI layouts differ. |
| `qclaw` | partial | `%APPDATA%/QClaw/qclaw.db` | `%APPDATA%/QClaw/IndexedDB/file__0.indexeddb.leveldb` | Audit rows can prove user input; full LevelDB recovery needs a dedicated parser. |
| `marvis` | partial | `%APPDATA%/Tencent/Marvis/db` | possible CEF LevelDB under `cef/CEF_Marvis` | App DB presence alone is not conversation evidence. |
| `lobsterai` | verified | `%APPDATA%/LobsterAI/lobsterai.sqlite` | SQLite message rows | Adjacent OpenClaw runtime can contain secrets. |
| `autoclaw` | partial | workspace JSON | `~/.openclaw-autoclaw/workspace/` | Do not assume every runtime JSON is a conversation. |
| `dumate` | verified | `%APPDATA%/qianfan-desktop-app/qianfan_desk_xdg/*/data/opencode/opencode.db` | OpenCode-style session → message → part rows | `~/.qianfan/workspace` is browser logging, not primary chat storage. |

## Product-specific schema clues

### Qoder / QoderCN

Observed safe `chat_session` columns include `session_id`, `session_title`, `project_uri`, `project_name`, `gmt_create`, `gmt_modified`, `session_type`, and `mode`. Discover columns dynamically before querying because releases can omit optional fields.

The metadata DB and plaintext transcript roots are independent. A session may be present in only one layer. Full session IDs can map exactly to full transcript stems; compact Quest/task transcripts use a bounded session-ID prefix. Compare all matches by parsed message coverage.

Legacy QoderCN IDE builds may keep chat-mode session state in `state.vscdb` `ItemTable` keys resembling `chat.chatMode.session.*`. Treat that route as version-specific.

### Codex

Use `state_5.sqlite` for discoverability/title state and rollout JSONL for source-of-truth message events. Preserve rollout path and event line. Archived sessions remain useful evidence and should be searched after active sessions.

### Claude Code

Use `history.jsonl` as a lightweight index when available, then map to project JSONL. Do not infer the exact original project path solely from a lossy encoded directory name when session metadata supplies the path.

### OpenCode and DuMate

OpenCode versions can use JSON storage, SQLite, or both. Detect the current layout before selecting. DuMate uses multiple workspace hashes with an OpenCode-style database under each workspace; enumerate only the known `qianfan_desk_xdg/*/data/opencode/opencode.db` pattern.

### VS Code-derived products

Trae, CodeBuddy and legacy QoderCN can store state in `state.vscdb`. Open the database read-only and inspect `ItemTable` keys before reading values. A generic workspace database contains large amounts of unrelated IDE state; only product-specific chat keys count as conversation evidence.

### Electron / LevelDB products

LevelDB or IndexedDB directories are not safely readable by treating `.ldb` files as text. Copying a live store can also produce inconsistent results. Discovery may report the root, but full recovery requires a dedicated read-only snapshot/parser and explicit user scope.

## Exclusions

Never treat these as conversation sources or export them with a history bundle:

- `auth.json`, `openclaw.json`, cookies and browser login databases;
- Electron local storage that has not been tied to a chat schema;
- telemetry, crash reports, update caches and model-download caches;
- encrypted fields whose key path or authorization is not part of the task;
- guessed product paths not backed by a local or upstream verification record.
