@echo off
rem AgentMemHub console entry (Windows). See start.sh later for other OS.
rem %~dp0 = directory of this script, so no absolute path is hardcoded.
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
