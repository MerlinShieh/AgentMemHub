@echo off
rem AgentMemHub SANDBOX console entry - feat/incremental-sync dev line.
rem Same launch as start.bat, but all writable data (db / watermarks /
rem scored state) stays inside the project under temp_path, so the real
rem data dir %USERPROFILE%\.agentmemhub is never touched.
rem Mechanism: AGENTMEM_HUB_DATA_DIR env var overrides yaml data_dir.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "AGENTMEM_HUB_DATA_DIR=%~dp0temp_path"
if not exist "temp_path" mkdir "temp_path"

where uv >nul 2>nul
if %errorlevel%==0 (
    uv run python -m agentmemhub
) else (
    python -m agentmemhub
)

endlocal
pause
