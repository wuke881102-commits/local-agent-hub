@echo off
chcp 65001 >nul
echo === 安装飞书 CLI (@larksuite/cli) ===
where npx >nul 2>nul
if errorlevel 1 (
  echo 未检测到 Node.js / npx。请先从 https://nodejs.org 安装 Node 20+。
  pause
  exit /b 1
)
echo.
echo 正在执行：npx @larksuite/cli@latest install
echo（首次安装可能耗时数分钟）
echo.
call npx @larksuite/cli@latest install
echo.
echo 安装完成后，运行以下命令完成首次授权：
echo     lark-cli auth login --recommend
echo.
pause
