#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT="$DIR/AgentCLI/AIConversationHubAgent"
if [ ! -x "$AGENT" ]; then
  echo "AIConversationHubAgent not found. Fully extract the release archive first."
  exit 1
fi
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null || true
"$AGENT" setup
if [ "${1:-}" != "--quiet" ]; then
  GUIDE="$HOME/Library/Application Support/AIConversationHub/UserData/AGENT_USAGE.md"
  [ -f "$GUIDE" ] && open "$GUIDE" 2>/dev/null || true
fi
if [ "${1:-}" != "--quiet" ] && [ -t 0 ]; then
  echo
  read -r -p "Press Enter to close…" _
fi
