@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

echo ============================================
echo  AnythingLLM upload page
echo  http://localhost:8080/
echo  Press Ctrl+C to stop.
echo ============================================
echo.

start "" "http://localhost:8080/"
"%PY%" -m http.server 8080 --bind 127.0.0.1
