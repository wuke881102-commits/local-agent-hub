#!/usr/bin/env bash
# 本地 Agent 工作台 · 一键启动（macOS / Linux）
set -e
cd "$(dirname "$0")/.."

echo
echo "======================================================"
echo "     本地 Agent 工作台  Local Agent Hub"
echo "     v0.1                              local-web"
echo "======================================================"
echo

# 1. Node 检测
if ! command -v node >/dev/null 2>&1; then
  echo "[错误] 未检测到 Node.js。请安装 Node 20+：https://nodejs.org/zh-cn/download"
  exit 1
fi
echo "[检查] Node.js $(node --version)"

# 2. Python 检测
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未检测到 python3。请安装 Python 3.11+：https://www.python.org/downloads/"
  exit 1
fi
echo "[检查] $(python3 --version)"

# 3. lark-cli 自动安装
if ! command -v lark-cli >/dev/null 2>&1; then
  echo
  echo "[初始化] 未检测到 lark-cli，开始首次安装 (预计 5-10 分钟)..."
  if npx -y @larksuite/cli@latest install; then
    echo "[完成] lark-cli 已安装。启动后请在网页中点击 '立即授权' 完成飞书授权。"
  else
    echo "[警告] lark-cli 安装失败。系统将以 mock 模式启动。"
  fi
else
  echo "[检查] lark-cli $(lark-cli --version 2>/dev/null || echo '?')"
fi

# 4. .env
[ -f backend/.env ] || { cp backend/.env.example backend/.env; echo "[初始化] 已创建 backend/.env，请稍后填入模型 Key"; }

# 5. Python venv + 后端依赖
if [ ! -d backend/.venv ]; then
  echo "[初始化] 创建 Python venv..."
  python3 -m venv backend/.venv
fi
echo "[安装] 后端依赖 (增量)..."
backend/.venv/bin/pip install --quiet --disable-pip-version-check --upgrade pip
backend/.venv/bin/pip install --quiet --disable-pip-version-check -e backend

# 6. 前端依赖
if [ ! -d frontend/node_modules ]; then
  echo "[安装] 前端依赖 (首次较慢)..."
  ( cd frontend && npm install --silent )
fi

# 7. 端口清理（POSIX）
for port in 8787 5173; do
  pid=$(lsof -ti tcp:$port 2>/dev/null || true)
  if [ -n "$pid" ]; then
    echo "[清理] 关闭已占用 $port 的进程 $pid"
    kill -9 $pid 2>/dev/null || true
  fi
done

# 8/9. 启动后端 + 前端（后台）
echo
echo "[启动] 后端 → http://127.0.0.1:8787"
( cd backend && ../backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --log-level info ) &
BACKEND_PID=$!

echo "[启动] 前端 → http://127.0.0.1:5173"
( cd frontend && npm run dev ) &
FRONTEND_PID=$!

# 10. 等热身
echo "[等待] 服务启动..."
for i in $(seq 1 25); do
  curl -sf http://127.0.0.1:8787/api/health >/dev/null 2>&1 && break
  sleep 1
done

# 11. 浏览器
if command -v open >/dev/null;     then open http://127.0.0.1:5173
elif command -v xdg-open >/dev/null; then xdg-open http://127.0.0.1:5173
else echo "请手动打开 http://127.0.0.1:5173"
fi

echo
echo "======================================================"
echo "  系统已启动"
echo
echo "  前端: http://127.0.0.1:5173"
echo "  后端: http://127.0.0.1:8787/docs"
echo
echo "  停止服务: 在本终端按 Ctrl+C"
echo "======================================================"

# 让脚本前台等候，Ctrl+C 时一起停子进程
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
wait
