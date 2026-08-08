@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   TE-Speed MiniMax H3 V3 Safe Uninstaller
echo ============================================================
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0TE-Speed-Launcher.ps1" -Action uninstall
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo ============================================================
  echo   SAFE UNINSTALL COMPLETE - fully restart ComfyUI.
  echo ============================================================
) else if "%RC%"=="1" (
  echo ============================================================
  echo   UNINSTALL COMPLETE WITH WARNING - read messages above.
  echo ============================================================
) else (
  echo ============================================================
  echo   UNINSTALL STOPPED SAFELY - conflicting files were preserved.
  echo ============================================================
)
echo.
pause
exit /b %RC%
