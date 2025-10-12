@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start_videocatalog.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if %EXITCODE% neq 0 (
    echo ------------------------------
    echo See the launcher log under %USERPROFILE%\VideoCatalog\logs
    pause >nul
)

endlocal & exit /b %EXITCODE%
