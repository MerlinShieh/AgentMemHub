# AI Conversation Hub — instructions for the installing Agent

The user has authorized installing AI Conversation Hub on this computer. Complete the installation locally and report the generated `AGENT_USAGE.md` path plus the valid discovered sources. Do not upload conversation data.

## Safety and destination

- Support Windows and macOS only.
- Preserve existing repositories, user data, Agent configs, and unrelated MCP blocks. Never force-reset or delete an existing install.
- Prefer a new default source directory when no canonical checkout exists:
  - Windows: `%LOCALAPPDATA%\AIConversationHub\Source`
  - macOS: `~/Library/Application Support/AIConversationHub/Source`
- The Hub reads vendor conversation stores only. Its own `sources.json`, notes, and generated usage guide live in the platform user-data directory.

## Choose an installation path

### A. Python 3.10+ is available (preferred for an Agent-managed source install)

1. If the destination is absent and Git is available, clone `https://github.com/Mark-Libetter/ai-conversation-hub.git` there. If a clean canonical checkout already exists, fetch and fast-forward only; if it is dirty or has a different remote, preserve it and use a new directory.
2. If Git is unavailable, download and extract `https://github.com/Mark-Libetter/ai-conversation-hub/archive/refs/heads/main.zip` into the destination.
3. From the repository root run:
   - Windows: `py -3 scripts/install_agent_access.py --json`
   - macOS: `python3 scripts/install_agent_access.py --json`

### B. Python is unavailable (use the self-contained Release)

1. Download the matching asset from `https://github.com/Mark-Libetter/ai-conversation-hub/releases/latest` and fully extract it.
2. Run the Agent integration helper:
   - Windows: `安装Agent接入.bat --quiet`
   - macOS: `install-agent.command --quiet` (clear quarantine only for this extracted folder if Gatekeeper requires it).

## Verify and report

1. Read the generated `AGENT_USAGE.md` completely.
2. Inspect `domestic_agents` in the installer JSON result:
   - WorkBuddy: when detected, verify both Skills under `~/.workbuddy/skills` and the merged `conversation-hub` entry in `~/.workbuddy/mcp.json`.
   - QwenWorkCN/千问办公 CLI: verify both Skills under `~/.qwenworkcn/skills`. If the current Agent exposes `mcp__qw-builtin__qw_query` and `mcp__qw-builtin__qw_action`, query `qwenwork.settings.connector.custom.conversation-hub`; add or update it using the returned `qwenwork_mcp_action.params.config`. Do not edit `mcp-adaptor.config` directly.
   - Qoder/QoderCN: when detected, verify both Skills under `~/.qoder/skills` or `~/.qoder-cn/skills` and the merged `conversation-hub` entry in that product's `mcp.json`.
   - QoderWork/千问办公桌面: verify both Skills under `~/.qoderwork/skills`; use the generated Agent CLI command because no stable external MCP file was verified.
3. Run the exact `ping` command written in `AGENT_USAGE.md`. It may start the local-only Hub automatically.
4. Confirm the Hub binds only to `127.0.0.1`, report which sources are valid, and tell the user to restart the detected Agents so Skill/MCP configuration reloads.
5. If an expected source is missing, report the detected path/status and let the user choose whether to provide an additional search root. Do not crawl unrelated disks automatically.
