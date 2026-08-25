# Traceable conversation recovery SOP

## 1. Define the target

Record whatever the user knows: product, approximate title, session ID, project, time range, and a distinctive phrase. Do not broaden to unrelated user profiles or disks.

## 2. Establish storage roles

Classify every known location as one of:

- `session_index`: titles, IDs, project and timestamps; not sufficient for review.
- `conversation_db`: both session and message records.
- `transcript_root`: plaintext JSON/JSONL/Markdown files.
- `runtime_root` or `context_only`: useful for diagnosis but not conversation evidence.
- `credential`: excluded from all reads and exports.

An application root existing is only a detection hint. A session/message schema or a parseable transcript is evidence.

## 3. Query the index safely

SQLite template:

```python
uri = db_path.resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.execute("PRAGMA query_only=ON")
```

Before selecting columns, inspect `sqlite_master` and `PRAGMA table_info`. Query only fields needed for mapping. If the current schema differs from the registry, report version drift; do not guess table contents.

Search exact IDs first, then title/project substrings and time windows. Retain the original metadata row as evidence.

## 4. Generate all plausible transcript candidates

Use deterministic mappings supported by the product format:

- exact session ID → exact file stem;
- documented short task ID → bounded session-ID prefix;
- project URI/hash → known project directory;
- index foreign key → message rows.

Do not accept arbitrary short prefixes. For Qoder, compact task stems must be 6–24 characters and prefix the indexed session ID.

## 5. Parse conservatively

- Keep only safe user/assistant text for coverage scoring.
- Skip thinking blocks, tool calls and tool results unless the user explicitly needs them.
- Strip injected system wrappers from user text.
- For Qoder, prefer the last `<user_query>` body when present; otherwise remove `<attached_files>` blocks.
- Count malformed lines and report them; never silently treat a partly parsed file as complete.

For each retained message, preserve:

```json
{
  "path": "absolute transcript path",
  "line": 42,
  "event_id": "uuid when present",
  "sha256_16": "first 16 hex characters of the raw-line SHA-256"
}
```

The short hash detects accidental source drift; it is not a signature or an anonymization method.

## 6. Score coverage and select

Evaluate every candidate. Rank by:

1. parsed user + assistant message count;
2. final evidence line (more recoverable events);
3. file modification time.

Always expose the candidate list and selection rule. A compact transcript can legitimately beat a full-format candidate when the latter is partial or stale.

## 7. State the recovery level

- `transcript`: a plaintext candidate was mapped and parsed.
- `metadata_only`: session metadata exists, but no readable transcript was mapped.
- `transcript_only`: a requested exact ID maps to a transcript after its index record disappeared.
- `not_detected`: no conversation-evidence location exists under known rules.
- `schema_drift`: a store exists but its expected schema is absent or incompatible.

Never translate `metadata_only` into “no conversation existed.”

## 8. Produce an evidence-backed retrospective

A compact retrospective card should contain:

- session title and ID;
- original goal (first valid user message);
- latest user request;
- latest assistant response after that request, if any;
- selected transcript path and coverage counts;
- evidence line/event/hash for each excerpt;
- known gaps and alternate candidates.

Previews should be short and redact obvious bearer tokens/API keys. For a complete export, require explicit user scope and use a task-specific exporter rather than printing full transcripts into the chat.

## Qoder mapping notes

Qoder has two independent layers:

1. `%APPDATA%/Qoder/SharedClientCache/cache/db/local.db`, table `chat_session`, provides safe title/ID/project/time metadata.
2. Plaintext transcript trees provide recoverable message bodies.

International edition:

- compact: `~/.qoder/cache/projects/*/conversation-history/<short-task>/<short-task>.jsonl`
- full: `~/.qoder/projects/*/transcript/<full-session-id>.jsonl`

QoderCN:

- compact: `~/.qoder-cn/cache/projects/*/conversation-history/<short-task>/<short-task>.jsonl`
- full/CLI: `%APPDATA%/QoderCN/SharedClientCache/cli/projects/**/*.jsonl`

`chat_message.content` is encrypted in observed versions and is outside this workflow. Do not decrypt it. The title index may cover sessions with no surviving plaintext transcript, so index count and recoverable transcript count must be reported separately.

## Grok Build mapping notes

Grok Build stores one directory per session under `$GROK_HOME/sessions/<encoded-cwd>/<session-id>/`. `GROK_HOME` defaults to `~/.grok`.

- `summary.json` is the index: session ID, title, cwd, timestamps, model. It is not the transcript.
- `updates.jsonl` is the recoverable conversation body. Keep `user_message_chunk` and `agent_message_chunk`; concatenate consecutive chunks of the same role.
- Skip `agent_thought_chunk`, `tool_call`, `tool_call_update`, `plan`, `session_recap`, and any path containing `subagents/`.
- `session_search.sqlite` is a derived FTS index. Report it as context-only; do not treat it as a transcript candidate.
- Never read `auth.json`.

If `summary.json` exists but `updates.jsonl` has no user/assistant text, the recovery level is `metadata_only`.
