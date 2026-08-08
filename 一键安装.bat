@echo off
setlocal EnableExtensions
chcp 65001 >nul

call :find_root
if not defined ROOT goto :fail
set "PY=%ROOT%\runtime\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python env not found: %PY%
  goto :fail
)

echo [OK] MiniMaxH3 root: %ROOT%
"%PY%" "%~dp0installer.py" install --root "%ROOT%"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto :success
if "%RC%"=="1" goto :warning
goto :fail

:success
echo.
echo ============================================================
echo   INSTALL COMPLETE - restart ComfyUI before use.
echo ============================================================
goto :end

:warning
echo.
echo ============================================================
echo   INSTALL COMPLETE WITH WARNING.
echo   Read the messages above. Core files were kept in a safe state.
echo ============================================================
goto :end

:fail
echo.
echo ============================================================
echo   INSTALL NOT COMPLETED. Unsafe overwrite was NOT performed.
echo ============================================================
goto :end

:find_root
set "ROOT="
if exist "%~dp0..\ComfyUI\comfy\ldm\minimax\model.py" for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not defined ROOT if exist "%~dp0ComfyUI\comfy\ldm\minimax\model.py" for %%I in ("%~dp0.") do set "ROOT=%%~fI"
for %%D in (C D E F G H) do if not defined ROOT if exist "%%D:\MiniMaxH3\ComfyUI\comfy\ldm\minimax\model.py" set "ROOT=%%D:\MiniMaxH3"
if not defined ROOT (
  echo MiniMaxH3 was not auto-detected.
  set /p "ROOT=Enter MiniMaxH3 root path: "
)
if defined ROOT if not exist "%ROOT%\ComfyUI\comfy\ldm\minimax\model.py" (
  echo [ERROR] Invalid MiniMaxH3 path: %ROOT%
  set "ROOT="
)
exit /b

:end
echo.
pause
endlocal
