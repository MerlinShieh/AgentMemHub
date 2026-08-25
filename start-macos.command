#!/bin/bash
# Double-click on a Mac to start the current AI Conversation Hub, including Grok Build.
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine "$PWD" 2>/dev/null || true
chmod +x "$0" 2>/dev/null || true

PY=""
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  osascript -e 'display dialog "未找到 python3。请先安装 Python 3.10+，例如：\n\nbrew install python" buttons {"好"} default button 1 with icon stop' 2>/dev/null
  exit 1
fi

"$PY" launcher.py
status=$?
if [ "$status" -ne 0 ]; then
  osascript -e "display dialog \"启动失败（退出码 $status）。请在终端运行：\\npython3 launcher.py\" buttons {\"好\"} default button 1 with icon stop" 2>/dev/null
  exit "$status"
fi
