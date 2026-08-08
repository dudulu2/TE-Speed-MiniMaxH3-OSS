@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   TE-Speed MiniMax H3 V3 Safe Installer
echo ============================================================
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0TE-Speed-Launcher.ps1" -Action install
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo ============================================================
  echo   INSTALL COMPLETE - fully restart ComfyUI before use.
  echo ============================================================
) else if "%RC%"=="1" (
  echo ============================================================
  echo   INSTALL COMPLETE WITH WARNING - read messages above.
  echo ============================================================
) else (
  echo ============================================================
  echo   INSTALL NOT COMPLETED - unsafe overwrite was not performed.
  echo ============================================================
)
echo.
pause
exit /b %RC%
