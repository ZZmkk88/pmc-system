@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PMC 系统 第三阶段

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] 第一次运行：正在创建虚拟环境并安装依赖，请等一两分钟……
  py -3.11 -m venv .venv || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt --no-cache-dir
)

echo [2/2] 启动服务，3 秒后自动打开浏览器 http://127.0.0.1:8000
echo 使用期间别关这个黑窗口（关了就等于停掉服务），按 Ctrl+C 也能停。
start "" /min powershell -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"
.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
pause
