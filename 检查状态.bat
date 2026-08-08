@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   TE-Speed MiniMax H3 V3 Status Check
echo ============================================================
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0TE-Speed-Launcher.ps1" -Action check
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo [PASS] TE-Speed core, node files and recognized workflows are healthy.
) else (
  echo [WARN] One or more TE-Speed checks did not pass. Read details above.
)
echo.
pause
exit /b %RC%
