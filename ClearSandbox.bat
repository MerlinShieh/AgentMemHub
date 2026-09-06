@echo off
rem =====================================================================
rem  ClearSandbox.bat - wipe the temp_path sandbox of feat/incremental-sync
rem
rem  Deletes (DESTRUCTIVE - sandbox only, NOT recoverable):
rem    1. temp_path  (sandbox db / watermarks / scored state)
rem    2. logs and exports (project artifacts, shared working dir)
rem
rem  Does NOT touch:
rem    - real data dir %USERPROFILE%\.agentmemhub
rem    - MemOS engine home memOS\home (engine data / config / password)
rem    - any agent source data
rem
rem  Usage (confirmation is the Y argument - deterministic, no prompt):
rem    ClearSandbox.bat Y          wipe
rem    ClearSandbox.bat            show this help, do nothing
rem =====================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "CONFIRM=%~1"
if /i not "%CONFIRM%"=="Y" (
    echo ClearSandbox - wipe temp_path sandbox data
    echo.
    echo Usage:  ClearSandbox.bat Y
    echo.
    echo Will delete: NOT recoverable
    echo   1. %CD%\temp_path
    echo   2. %CD%\logs  and  %CD%\exports
    echo Keeps: real data dir, engine home, all agent sources.
    echo.
    echo Pass Y as the first argument to actually wipe.
    exit /b 1
)

echo ClearSandbox - wipe sandbox data
echo [1/2] Wiping temp_path...
if exist "temp_path" (
    rmdir /s /q "temp_path"
    echo   - deleted temp_path
) else (
    echo   - temp_path not found, skipped
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
echo [OK] Sandbox clean.
endlocal
exit /b 0
