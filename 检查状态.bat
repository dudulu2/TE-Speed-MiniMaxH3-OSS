@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "ROOT="
if exist "%~dp0..\ComfyUI\comfy\ldm\minimax\model.py" for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not defined ROOT if exist "%~dp0ComfyUI\comfy\ldm\minimax\model.py" for %%I in ("%~dp0.") do set "ROOT=%%~fI"
for %%D in (C D E F G H) do if not defined ROOT if exist "%%D:\MiniMaxH3\ComfyUI\comfy\ldm\minimax\model.py" set "ROOT=%%D:\MiniMaxH3"
if not defined ROOT set /p "ROOT=Enter MiniMaxH3 root path: "
set "PY=%ROOT%\runtime\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python env not found
  goto :end
)
"%PY%" "%~dp0installer.py" check --root "%ROOT%"
:end
pause
