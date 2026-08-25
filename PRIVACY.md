# Privacy

AI Conversation Hub Lite is local-first. Its server binds only to `127.0.0.1` —
there is no LAN listener, no device pairing, and no remote access path of any
kind.

Original conversation stores are opened read-only and are never renamed,
archived, deleted, or modified.

The Hub indexes only top-level user and assistant text. System/developer
prompts, reasoning, tool input/output, background notifications, subagent-only
records (for example ZCode subagent sessions and Codex subagent threads), and
common secret patterns (Bearer tokens, API keys, passwords) are excluded.

Hub-owned notes, tags, favorites, and daily-review metadata are stored in a
separate `hub_notes.sqlite`, apart from all source-product databases.
Management backups export only Hub-owned data and never include original
conversations or machine-specific source settings.

Search and the rule-based daily review are fully deterministic and offline: no
model endpoint is ever contacted, and the Lite build has no model features.
