@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Local Agent Hub

cd /d "%~dp0\.."

echo.
echo ======================================================
echo   Local Agent Hub  -  v0.1  -  local-web
echo ======================================================
echo.

REM ----- 1. Node.js -----
where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js not found.
  echo.
  echo Please install Node.js 20+ first:
  echo     https://nodejs.org/en/download
  echo Check "Add to PATH" during install.
  echo.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('node --version') do set NODEVER=%%v
echo [OK] Node.js !NODEVER!

REM ----- 2. Python -----
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found.
  echo.
  echo Please install Python 3.11+ first:
  echo     https://www.python.org/downloads/
  echo Check "Add Python to PATH" during install.
  echo.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] !PYVER!

REM ----- 3. lark-cli (auto-install on first run) -----
set "PATH=%APPDATA%\npm;%PATH%"
where lark-cli >nul 2>nul
if errorlevel 1 (
  echo.
  echo ======================================================
  echo  [INIT] lark-cli not installed. Starting installer...
  echo ======================================================
  echo.
  echo  Install will show an interactive prompt with strange
  echo  characters like "?" or block symbols -- that is normal,
  echo  cmd.exe cannot render those Unicode decoration chars.
  echo.
  echo  At each prompt:  press [Enter] to accept defaults
  echo                or use [Up/Down arrows] + [Enter]
  echo                or [Space] to toggle multi-select
  echo.
  echo  The installer will also show a QR code at the end
  echo  to bind lark-cli to your Feishu account. SCAN IT.
  echo  ^(or copy the URL below the QR into a browser^)
  echo.
  echo  Press any key to begin install ^(takes 5-10 min^)...
  pause >nul
  echo.

  set "CI=1"
  set "FORCE_COLOR=0"
  set "npm_config_yes=true"

  call npx -y @larksuite/cli@latest install
  chcp 65001 >nul
  set "PATH=%APPDATA%\npm;%PATH%"
  where lark-cli >nul 2>nul
  if errorlevel 1 (
    echo.
    echo [WARN] lark-cli install failed or was cancelled.
    echo        System will start in mock mode.
    timeout /t 3 >nul
  ) else (
    echo.
    echo [OK] lark-cli installed.
    timeout /t 2 >nul
  )
) else (
  for /f "tokens=*" %%v in ('lark-cli --version 2^>nul') do set LARKVER=%%v
  echo [OK] lark-cli !LARKVER!
)

REM ----- 4. .env -----
if not exist "backend\.env" (
  echo [INIT] Creating backend\.env from .env.example
  copy /Y "backend\.env.example" "backend\.env" >nul
)

REM ----- 5. Python venv + backend deps -----
if not exist "backend\.venv" (
  echo [INIT] Creating Python venv ...
  python -m venv backend\.venv
  if errorlevel 1 (
    echo [ERROR] venv creation failed.
    pause & exit /b 1
  )
)
echo [INSTALL] Backend dependencies ^(incremental^)...
"backend\.venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade pip
"backend\.venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e backend
if errorlevel 1 (
  echo [ERROR] Backend dependency install failed.
  pause & exit /b 1
)

REM ----- 6. Frontend deps + build -----
if not exist "frontend\node_modules" (
  echo [INSTALL] Frontend dependencies ^(first run, 1-3 min^)...
  pushd frontend
  call npm install --silent
  popd
  if errorlevel 1 (
    echo [ERROR] Frontend dependency install failed.
    pause & exit /b 1
  )
)

REM Build frontend if dist missing OR any src file newer than dist
set "NEED_BUILD="
if not exist "frontend\dist\index.html" set "NEED_BUILD=1"
if defined NEED_BUILD (
  echo [BUILD] Building frontend ^(one-time, ~30s^)...
  pushd frontend
  call npm run build
  popd
  if errorlevel 1 (
    echo [ERROR] Frontend build failed.
    pause & exit /b 1
  )
) else (
  echo [OK] Frontend already built ^(frontend\dist^)
)

REM ----- 7. Free 8787 -----
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8787.*LISTENING"') do (
  echo [CLEAN] Killing PID %%p on port 8787
  taskkill /PID %%p /F >nul 2>nul
)

REM ----- 8. Open browser then run backend in foreground -----
echo.
echo ======================================================
echo  Starting Local Agent Hub on http://127.0.0.1:8787
echo  ^(this single window hosts the WHOLE app^)
echo.
echo  Stop service: close this window or press Ctrl+C
echo ======================================================
echo.

REM Open browser shortly after backend starts
start "" /B cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8787"

REM Run uvicorn IN THE FOREGROUND of this window.
REM When user closes this window, uvicorn dies, everything stops.
"backend\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --log-level info

endlocal
