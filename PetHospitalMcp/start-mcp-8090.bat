@echo off
rem 一键启动宠物医院 MCP 服务（Go REST 后端 + MCP 网关，端口 8090）
start "PetHospital Go 后端 (8080)" cmd /k "cd /d E:\pet-hospital-windows-amd64\windows && pethospital.exe"
timeout /t 3 >nul
start "PetHospitalMCP (8090)" cmd /k "cd /d E:\LLM\PetHospitalMcp && set MCP_HOST=127.0.0.1&& set MCP_PORT=8090&& set PYTHONPATH=E:\LLM\PetHospitalMcp\src&& .venv\Scripts\python.exe -m pet_hospital_mcp"
echo 两个服务已在独立窗口启动：Go 后端 8080，MCP 网关 8090/mcp
echo 然后双击 E:\skill\start-xiaoA.bat 启动 xiaoA 即可使用 MCP 工具。
pause
