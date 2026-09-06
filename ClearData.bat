@echo off
rem =====================================================================
rem  ClearData.bat - wipe AgentMemHub's own data (fresh-start for the app)
rem
rem  Deletes (DESTRUCTIVE - NOT recoverable):
rem    1. database  (agentmemhub.db / watermarks / scored_traces.json)
rem    2. logs and exports (project artifacts)
rem
rem  Does NOT touch:
rem    - MemOS engine home memOS\home (engine data / config / password)
rem    - any agent source data
rem    - legacy data dir %USERPROFILE%\.agentmemhub (old installs)
rem
rem  Usage (confirmation is the Y argument - deterministic, no prompt):
rem    ClearData.bat Y          wipe
rem    ClearData.bat            show this help, do nothing
rem =====================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "CONFIRM=%~1"
if /i not "%CONFIRM%"=="Y" (
    echo ClearData - wipe AgentMemHub data dir + project artifacts
    echo.
    echo Usage:  ClearData.bat Y
    echo.
    echo Will delete: NOT recoverable
    echo   1. %CD%\database
    echo   2. %CD%\logs  and  %CD%\exports
    echo Keeps: engine home, all agent sources, legacy data dir.
    echo.
    echo Pass Y as the first argument to actually wipe.
    exit /b 1
)

echo ClearData - wipe AgentMemHub data
echo [1/2] Wiping database...
if exist "database" (
    rmdir /s /q "database"
    echo   - deleted database
) else (
    echo   - database not found, skipped
)

echo [2/2] Wiping project artifacts (logs / exports)...
if exist "logs" (
    rmdir /s /q "logs"
    echo   - deleted logs
)
if exist "exports" (
    rmdir /s /q "exports"
    echo   - deleted exports
)

echo.
echo [OK] Clean state ready. Next ingest rebuilds from agent sources.
endlocal
exit /b 0
