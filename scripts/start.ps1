# PowerShell 启动脚本（功能等同 start.cmd）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "=== 本地 Agent 工作台 启动 ===" -ForegroundColor Green

# 1) lark-cli 检测
if (-not (Get-Command lark-cli -ErrorAction SilentlyContinue)) {
  Write-Warning "未检测到 lark-cli，系统将运行在 mock 模式。安装：npx @larksuite/cli@latest install"
}

# 2) .env 检测
if (-not (Test-Path "backend\.env")) {
  Write-Host "复制 .env.example -> backend\.env"
  Copy-Item "backend\.env.example" "backend\.env"
}

# 3) Python 虚拟环境
if (-not (Test-Path "backend\.venv")) {
  Write-Host "[1/4] 创建 Python venv ..."
  python -m venv backend\.venv
}
Write-Host "[2/4] 安装后端依赖 ..."
& "backend\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "backend\.venv\Scripts\python.exe" -m pip install --quiet -e backend

# 4) 启动后端
Write-Host "[3/4] 启动后端 127.0.0.1:8787 ..."
Start-Process -FilePath cmd -ArgumentList "/k", "cd /d $root\backend && ..\backend\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload"

# 5) 前端
if (-not (Test-Path "frontend\node_modules")) {
  Write-Host "安装前端依赖 ..."
  Push-Location frontend
  npm install
  Pop-Location
}
Write-Host "[4/4] 启动前端 127.0.0.1:5173 ..."
Start-Process -FilePath cmd -ArgumentList "/k", "cd /d $root\frontend && npm run dev"

Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:5173"
Write-Host "完成。"
