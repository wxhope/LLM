@echo off
chcp 65001 >nul
title xiaoA

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] creating venv ...
    python -m venv .venv
    if errorlevel 1 goto :nofound
    echo [2/2] installing deps ...
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 goto :pipfail
)

".venv\Scripts\python.exe" -X utf8 main.py %*
set EXITCODE=%errorlevel%
if not "%EXITCODE%"=="0" goto :failed
goto :eof

:nofound
echo.
echo [ERROR] python not found. Install Python 3.10+ and tick Add to PATH.
pause
exit /b 1

:pipfail
echo.
echo [ERROR] pip install failed. Check network and retry.
pause
exit /b 1

:failed
echo.
echo xiaoA exited with code %EXITCODE%
pause
exit /b %EXITCODE%
