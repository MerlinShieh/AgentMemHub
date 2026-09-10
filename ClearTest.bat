@echo off
rem =====================================================================
rem  ClearTest.bat - wipe AgentMemHub test data back to a clean environment
rem
rem  Deletes (DESTRUCTIVE - test only, NOT recoverable):
rem    1. database\  (collection DB agentmemhub.db + rag index session_rag.db
rem       + watermarks.json + scored_traces.json + config.json)
rem    2. Project artifacts: logs\ and exports\
rem
rem  Keeps (so the vendored MemOS fallback engine survives):
rem    - memOS\home\config.yaml
rem    - viewer password / auth files under memOS\home
rem
rem  Note: this also removes memOS\home\data (the old MemOS memory DB).
rem  With backend=rag the memory index lives in database\session_rag.db,
rem  so that is what matters for a clean test run.
rem
rem  Usage (confirmation is the Y argument - deterministic, no prompt):
rem    ClearTest.bat Y          wipe everything
rem    ClearTest.bat            show this help, do nothing
rem
rem  Precondition: no panel/process should hold database\. Stop the panel
rem  first with ClearPanel.bat or Ctrl+C in its window. The rag engine is
rem  in-process, so there is no daemon to stop.
rem =====================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "DATA_DIR=%CD%\database"
set "ENGINE_HOME=memOS\home"

set "CONFIRM=%~1"
if /i not "%CONFIRM%"=="Y" (
    echo ClearTest - wipe AgentMemHub test data
    echo.
    echo Usage:  ClearTest.bat Y
    echo.
    echo Will delete: NOT recoverable
    echo   1. %DATA_DIR%
    echo       collection DB agentmemhub.db
    echo       rag index session_rag.db
    echo       watermarks.json / scored_traces.json / config.json
    echo   2. %CD%\logs  and  %CD%\exports
    echo   3. memOS\home data / logs / daemon   [fallback engine only]
    echo Keeps: memOS\home\config.yaml and viewer password
    echo.
    echo Pass Y as the first argument to actually wipe.
    exit /b 1
)

echo ClearTest - wipe AgentMemHub test data
echo [1/4] Checking no panel is holding the database ...
netstat -ano | findstr ":8086" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [!] Panel port 8086 is still listening. Stop the panel first
    echo     then re-run. Aborted, nothing deleted.
    exit /b 1
)

echo [2/4] Wiping AgentMemHub data dir ...
if exist "%DATA_DIR%" (
    rmdir /s /q "%DATA_DIR%"
    echo   - deleted %DATA_DIR%
) else (
    echo   - %DATA_DIR% not found, skipped
)

echo [3/4] Wiping project artifacts - logs / exports ...
if exist "logs" (
    rmdir /s /q "logs"
    echo   - deleted logs
)
if exist "exports" (
    rmdir /s /q "exports"
    echo   - deleted exports
)

echo [4/4] Wiping fallback engine data - data / logs / daemon ...
if exist "%ENGINE_HOME%\data" (
    rmdir /s /q "%ENGINE_HOME%\data"
    echo   - deleted %ENGINE_HOME%\data
) else (
    echo   - %ENGINE_HOME%\data not found, skipped
)
if exist "%ENGINE_HOME%\logs" (
    rmdir /s /q "%ENGINE_HOME%\logs"
)
if exist "%ENGINE_HOME%\daemon" (
    rmdir /s /q "%ENGINE_HOME%\daemon"
)

echo.
echo [OK] Clean environment ready.
echo Next steps to verify from scratch:
echo   1. Collect   :  python -m agentmemhub sync
echo   2. Panel     :  start.bat   then open http://127.0.0.1:8086
echo   3. Or MCP    :  python -m agentmemhub mcp
endlocal
exit /b 0
