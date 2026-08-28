@echo off
rem AgentMemHub 控制台入口（Windows；后续系统可对照做 start.sh）
rem 无绝对路径依赖：%~dp0 = 本脚本所在目录，自动定位项目根
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel%==0 (
    uv run python -m agentmemhub
) else (
    python -m agentmemhub
)

endlocal
pause
