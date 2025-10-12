@echo off
setlocal
set SCRIPT_DIR=%~dp0

for %%P in (powershell.exe powershell pwsh.exe pwsh) do (
    where %%P >nul 2>nul
    if not errorlevel 1 (
        set PS_BIN=%%P
        goto :found
    )
)
echo PowerShell not found. Please install PowerShell 5 or newer.
endlocal & exit /b 1

:found
"%PS_BIN%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start_videocatalog.ps1" %*
set EXITCODE=%ERRORLEVEL%
endlocal & exit /b %EXITCODE%
