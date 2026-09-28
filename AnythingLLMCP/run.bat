@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  AnythingLLM MCP Server
echo  Endpoint : http://127.0.0.1:8765/mcp
echo  Health   : http://127.0.0.1:8765/health
echo  Press Ctrl+C to stop.
echo ============================================
echo.

".venv\Scripts\python.exe" -m anythingllm_mcp

echo.
echo Server stopped.
pause
