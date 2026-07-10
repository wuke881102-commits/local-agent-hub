@echo off
REM ============================================================
REM  One-command, end-to-end installer build.
REM  Safe to re-run: the bundled runtime (Node + lark-cli) is
REM  bootstrapped only when missing, so repeat builds are fast.
REM
REM   1. Ensure Python build deps (pyinstaller, pystray, pillow)
REM   2. Bootstrap bundled runtime (Node + @larksuite/cli)
REM   3. Build frontend (npm run build)
REM   4. Compile backend with PyInstaller
REM   5. Stage everything to build\stage\
REM   6. Compile Inno Setup installer to dist-installer\
REM ============================================================

setlocal enabledelayedexpansion
chcp 65001 >nul
title Build LocalAgentHub Installer

cd /d "%~dp0\.."
set "ROOT=%CD%"
set "PY=backend\.venv\Scripts\python.exe"
set "NODEVER=node-v20.18.1-win-x64"
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"

echo.
echo ============================================================
echo  LocalAgentHub Installer Build
echo  Root: %ROOT%
echo ============================================================
echo.

REM ----- Sanity checks -----
where node >nul 2>nul || (echo [ERROR] Node.js missing & exit /b 1)
where npm  >nul 2>nul || (echo [ERROR] npm missing & exit /b 1)
if not exist "%PY%" (
  echo [ERROR] backend\.venv missing. Run scripts\start.cmd once to bootstrap.
  exit /b 1
)
if not exist "%ISCC%" (
  echo [ERROR] Inno Setup not installed. Download from https://jrsoftware.org/isdl.php
  exit /b 1
)

REM ----- 1. Python build deps -----
echo [1/6] Ensuring Python build deps (pyinstaller, pystray, pillow)...
"%PY%" -m pip install --quiet --disable-pip-version-check pyinstaller pystray pillow
if errorlevel 1 (echo [ERROR] failed to install Python build deps & exit /b 1)

REM ----- 2. Bootstrap bundled runtime (Node + lark-cli) -----
echo [2/6] Bootstrapping bundled runtime...
if not exist "build\stage\runtime" mkdir "build\stage\runtime"

if not exist "build\stage\runtime\node\node.exe" (
  echo   - Extracting bundled Node ^(%NODEVER%^)...
  if not exist "build\tools\%NODEVER%.zip" (
    echo [ERROR] build\tools\%NODEVER%.zip missing.
    exit /b 1
  )
  if exist "build\stage\runtime\%NODEVER%" rmdir /S /Q "build\stage\runtime\%NODEVER%"
  powershell -NoProfile -Command "Expand-Archive -Path 'build\tools\%NODEVER%.zip' -DestinationPath 'build\stage\runtime' -Force"
  if errorlevel 1 (echo [ERROR] Node extract failed & exit /b 1)
  if exist "build\stage\runtime\node" rmdir /S /Q "build\stage\runtime\node"
  move "build\stage\runtime\%NODEVER%" "build\stage\runtime\node" >nul
) else (
  echo   - Bundled Node already present, skipping.
)

REM NOTE: the real Feishu CLI is the SCOPED package @larksuite/cli.
REM       (the public "lark-cli" package is an unrelated squatter - do NOT use it.)
if not exist "build\stage\runtime\lark-cli\node_modules\.bin\lark-cli.cmd" (
  echo   - Installing bundled lark-cli ^(@larksuite/cli^)...
  if not exist "build\stage\runtime\lark-cli" mkdir "build\stage\runtime\lark-cli"
  pushd "build\stage\runtime\lark-cli"
  call npm install @larksuite/cli --silent
  if errorlevel 1 (echo [ERROR] lark-cli install failed & popd & exit /b 1)
  popd
) else (
  echo   - Bundled lark-cli already present, skipping.
)

REM ----- 3. Build frontend -----
echo [3/6] Building frontend...
pushd frontend
if not exist "node_modules" call npm install --silent
call npm run build
if errorlevel 1 (echo [ERROR] frontend build failed & popd & exit /b 1)
popd

REM ----- 4. Build backend (PyInstaller) -----
echo [4/6] Building backend with PyInstaller...
pushd backend
.venv\Scripts\python.exe -m PyInstaller launcher.spec --noconfirm --clean
if errorlevel 1 (echo [ERROR] PyInstaller build failed & popd & exit /b 1)
popd

REM ----- 5. Stage tree -----
echo [5/6] Staging install tree...
REM build\stage\runtime is preserved (bootstrapped in step 2).
if not exist "build\stage\runtime\node\node.exe" (
  echo [ERROR] build\stage\runtime\node missing after bootstrap - see step 2.
  exit /b 1
)
if exist "build\stage\backend"  rmdir /S /Q "build\stage\backend"
if exist "build\stage\frontend" rmdir /S /Q "build\stage\frontend"
if exist "build\stage\config"   rmdir /S /Q "build\stage\config"
xcopy /E /I /Q /Y "backend\dist\LocalAgentHub" "build\stage\backend" >nul
xcopy /E /I /Q /Y "frontend\dist"             "build\stage\frontend\dist" >nul
xcopy /E /I /Q /Y "config"                    "build\stage\config" >nul
copy /Y "build\production.env" "build\stage\backend\.env" >nul

REM ----- 6. Compile installer -----
echo [6/6] Compiling Inno Setup installer...
"%ISCC%" "build\installer.iss"
if errorlevel 1 (echo [ERROR] Inno Setup compile failed & exit /b 1)

echo.
echo ============================================================
echo  Done.  Installer at: dist-installer\ ^(LocalAgentHub-Setup-^<version^>.exe^)
echo ============================================================
echo.
endlocal
