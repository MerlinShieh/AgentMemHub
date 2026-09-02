@echo off
rem =====================================================================
rem  ClearTest.bat - wipe AgentMemHub test data back to a clean environment
rem
rem  Deletes (DESTRUCTIVE - test only, NOT recoverable):
rem    1. AgentMemHub unified conversation DB dir (%USERPROFILE%\.agentmemhub)
rem       - agentmemhub.db / events / FTS / scored_traces.json / sync anchor
rem    2. Project artifacts: logs\ (operation logs) and exports\
rem    3. MemOS engine data: memOS\home\data\ (memos.db + WAL),
rem       memOS\home\logs\ (engine logs), memOS\home\daemon\ (bridge state)
rem
rem  Keeps (so config survives):
rem    - memOS\home\config.yaml (embedding model bge config)
rem    - viewer password / auth files under memOS\home
rem    - local embedding model files in node_modules
rem
rem  Usage (confirmation is the Y argument - deterministic, no prompt):
rem    ClearTest.bat Y          wipe everything
rem    ClearTest.bat            show this help, do nothing
rem
rem  Precondition: engine must be stopped. Tries `memos-daemon stop` first;
rem  if port 18800 still listens (engine started another way) it ABORTS.
rem =====================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "DATA_DIR=%USERPROFILE%\.agentmemhub"
set "ENGINE_HOME=memOS\home"

set "CONFIRM=%~1"
if /i not "%CONFIRM%"=="Y" (
    echo ClearTest - wipe AgentMemHub / MemOS test data
    echo.
    echo Usage:  ClearTest.bat Y
    echo.
    echo Will delete: NOT recoverable
    echo   1. %DATA_DIR%
    echo   2. %CD%\logs  and  %CD%\exports
    echo   3. %ENGINE_HOME%\data , logs , daemon
    echo Keeps: %ENGINE_HOME%\config.yaml, viewer password, embedding models
    echo.
    echo Pass Y as the first argument to actually wipe.
    exit /b 1
)

echo ClearTest - wipe AgentMemHub / MemOS test data
echo [1/4] Stopping memory engine (memos-daemon stop)...
where uv >nul 2>nul
if %errorlevel%==0 (
    uv run python -m agentmemhub memos-daemon stop >nul 2>&1
) else (
    python -m agentmemhub memos-daemon stop >nul 2>&1
)
ping -n 3 127.0.0.1 >nul

netstat -ano | findstr ":18800" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [!] Port 18800 is still listening - engine is running and was not
    echo     started by this tool. Stop it first via dashboard or by ending
    echo     the process, then re-run. Aborted, nothing deleted.
    exit /b 1
)

echo [2/4] Wiping AgentMemHub data dir...
if exist "%DATA_DIR%" (
    rmdir /s /q "%DATA_DIR%"
    echo   - deleted %DATA_DIR%
) else (
    echo   - %DATA_DIR% not found, skipped
)

echo [3/4] Wiping project artifacts (logs / exports)...
if exist "logs" (
    rmdir /s /q "logs"
    echo   - deleted logs
)
if exist "exports" (
    rmdir /s /q "exports"
    echo   - deleted exports
)

echo [4/4] Wiping engine memory data (data / logs / daemon)...
if exist "%ENGINE_HOME%\data" (
    rmdir /s /q "%ENGINE_HOME%\data"
    echo   - deleted %ENGINE_HOME%\data
) else (
    echo   - %ENGINE_HOME%\data not found, skipped
)
if exist "%ENGINE_HOME%\logs" (
    rmdir /s /q "%ENGINE_HOME%\logs"
    echo   - deleted %ENGINE_HOME%\logs
)
if exist "%ENGINE_HOME%\daemon" (
    rmdir /s /q "%ENGINE_HOME%\daemon"
    echo   - deleted %ENGINE_HOME%\daemon
)

echo.
echo [OK] Clean environment ready.
echo Next steps to verify from scratch:
echo   1. Start engine :  python -m agentmemhub memos-daemon start
echo   2. Ingest       :  python -m agentmemhub ingest
echo   3. Push memory  :  python -m agentmemhub memos --push http://127.0.0.1:18800
echo   4. Score        :  python -m agentmemhub score --sync-episodes
echo   5. Check        :  http://127.0.0.1:18800/#/memories
endlocal
exit /b 0