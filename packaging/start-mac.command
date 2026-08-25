#!/bin/bash
# AI Conversation Hub · macOS 首次运行引导
# 双击此文件，会自动清除 macOS 隔离属性并弹图形对话框引导你打开 App。
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

APP="AIConversationHub.app"

# ---------- 0. 定位 .app ----------
if [ ! -d "$APP" ]; then
  osascript -e 'display dialog "没有找到 AIConversationHub.app。\n\n请确认：你已经把整个压缩包【完整解压】到一个文件夹，然后把这个文件（start-mac.command）和 AIConversationHub.app 放在同一个文件夹里。" with title "AI Conversation Hub" buttons {"好"} default button 1 with icon stop' 2>/dev/null
  exit 1
fi

# ---------- 1. 自动清除 macOS 隔离属性 ----------
# 从网络下载/微信接收的文件会被打上 com.apple.quarantine，导致 .app 提示
# 「已损坏，无法打开」（其实没坏）。清除自己用户的文件不需要 sudo。
echo "正在准备运行环境..."
xattr -dr com.apple.quarantine "$DIR" 2>/dev/null || true
chmod +x "$APP/Contents/MacOS/AIConversationHub" 2>/dev/null
chmod +x "$APP" 2>/dev/null

# ---------- 2. 弹图形对话框引导首次右键打开 ----------
osascript <<'APPLESCRIPT' 2>/dev/null
tell application "Finder"
    activate
    set folderPath to (POSIX file (do shell script "pwd")) as alias
    open folderPath
    select file "AIConversationHub.app" of folderPath
end tell

display dialog "AI Conversation Hub 已就绪！\n\n接下来：\n1. 在刚打开的文件夹窗口里，找到 AIConversationHub.app\n2. 【右键】点击它（或按住 Control 点一下）\n3. 选「打开」\n4. 弹出提示时，再点「打开」\n\n（首次需要这样操作一次，以后双击就能直接用了）" with title "AI Conversation Hub · 首次运行" buttons {"我知道了"} default button 1 with icon note
APPLESCRIPT

# ---------- 3. 启动并校验 ----------
echo "正在启动 AI Conversation Hub..."
open "$APP" 2>/dev/null
sleep 3

if pgrep -f "AIConversationHub" >/dev/null 2>&1; then
  echo "✅ 启动成功！浏览器应该已经自动打开。"
  echo "（如果浏览器没自动开，手动访问 http://127.0.0.1:8765）"
else
  echo ""
  echo "⚠️  启动似乎没有成功。"
  echo ""
  echo "如果系统提示「已损坏，无法打开」，请在终端执行这条命令后再试："
  echo "  xattr -cr \"$DIR\""
  echo ""
  echo "然后右键 AIConversationHub.app → 打开 → 仍要打开。"
fi

# ---------- 4. 等用户看清结果再关闭窗口 ----------
echo ""
read -r -p "按回车键关闭此窗口…" _
