@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PMC System Phase3 (PMC xi tong)

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] First run: creating venv and installing dependencies, please wait...
  py -3.11 -m venv .venv || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt --no-cache-dir
)

echo [2/2] Starting PMC server; browser will open http://127.0.0.1:8000 in 3s
echo Keep this window open = server running. Close window / Ctrl+C = stop server.
start "" /min powershell -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"
.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
pause
