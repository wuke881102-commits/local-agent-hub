@echo off
REM Development mode: opens two windows (Vite HMR + uvicorn --reload).
REM Normal users should use start.cmd or 启动飞书Agent.bat instead.
setlocal enabledelayedexpansion
chcp 65001 >nul
title Local Agent Hub - Dev

cd /d "%~dp0\.."
set "PATH=%APPDATA%\npm;%PATH%"

echo.
echo === Local Agent Hub - DEV MODE ===
echo Frontend Vite HMR  -> http://127.0.0.1:5173
echo Backend uvicorn    -> http://127.0.0.1:8787
echo.

REM Kill stale processes
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8787.*LISTENING"') do taskkill /PID %%p /F >nul 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5173.*LISTENING"') do taskkill /PID %%p /F >nul 2>nul

REM Frontend deps
if not exist "frontend\node_modules" (
  pushd frontend & call npm install & popd
)

echo Starting backend ^(--reload^)...
start "feishu-backend (dev)" cmd /c ^
  "cd /d %CD%\backend && ..\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload"

echo Starting frontend ^(vite^)...
start "feishu-frontend (dev)" cmd /c ^
  "cd /d %CD%\frontend && npm run dev"

timeout /t 5 >nul
start "" http://127.0.0.1:5173

echo.
echo Two dev windows are running. Close them individually to stop.
echo Press any key to close this launcher window.
pause >nul
endlocal
