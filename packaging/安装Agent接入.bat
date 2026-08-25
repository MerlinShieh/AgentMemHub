@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "AgentCLI\AIConversationHubAgent.exe" (
  echo 未找到 AgentCLI\AIConversationHubAgent.exe，请完整解压安装包。
  exit /b 1
)
"AgentCLI\AIConversationHubAgent.exe" setup
if errorlevel 1 exit /b %ERRORLEVEL%
if /I "%~1"=="--quiet" exit /b 0
if exist "%LOCALAPPDATA%\AIConversationHub\UserData\AGENT_USAGE.md" start "" "%LOCALAPPDATA%\AIConversationHub\UserData\AGENT_USAGE.md"
exit /b 0
