"""FastAPI 入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .feishu import get_lark, LarkCLI

# 注册所有 agent（import 触发 register_agent 副作用）
from . import agents  # noqa: F401
from .routes import auth, scenes, agents as agents_route, tasks, assets, writeback, diagnostics, org, base, dispatch, summaries, localdir, autoextract

# Single source of truth for the backend version. Bump via
# scripts\bump_version.ps1 <new-version> (keeps the frontend + installer in sync).
APP_VERSION = "5.5"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from .services import task_runner
    reaped = await task_runner.reap_orphans()
    if reaped:
        logging.warning("Reaped %d orphaned 'running' task(s) left by previous shutdown.", reaped)
    lark = await get_lark()
    available = await lark.ping()
    # 打包内置专用应用时：启动即确保 lark-cli 用的就是这个应用（旧机器/默认 Feishubot 会切换过来）。
    # 这样后续所有调用都走专用应用；已登录旧 app 的用户会变成"未登录"，被引导重新做最小授权。
    if isinstance(lark, LarkCLI) and available:
        try:
            await lark.ensure_app_configured()
        except Exception:
            logging.exception("ensure_app_configured failed at startup")
    logging.info("Startup ok. lark-cli available=%s mock_fallback=%s text_model=%s",
                 available, settings.enable_mock_fallback, settings.text_model)
    yield


app = FastAPI(
    title="Local Agent Hub · Local Backend",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_dev_origin],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(scenes.router)
app.include_router(agents_route.router)
app.include_router(tasks.router)
app.include_router(assets.router)
app.include_router(writeback.router)
app.include_router(diagnostics.router)
app.include_router(org.router)
app.include_router(base.router)
app.include_router(dispatch.router)
app.include_router(summaries.router)
app.include_router(localdir.router)
app.include_router(autoextract.router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": APP_VERSION}


# 生产模式：把 frontend/dist 挂载到 / 提供同源访问。
# 同时为 SPA 路由（/diagnostics、/assets 等）添加 fallback：找不到静态文件时返回 index.html。
FRONTEND_DIST = settings.frontend_dist
if FRONTEND_DIST.exists():
    # /assets 挂载真实静态资源（JS/CSS/PNG）
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request):
        # API 路径不在此处理（路由器已注册在前，正常情况不会落到这里）
        if full_path.startswith("api/"):
            return JSONResponse({"error": "not_found", "path": full_path}, status_code=404)
        # 静态资源：试着按路径直接返回文件（带哈希，可长期缓存）
        candidate = FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        # 其它一律返回 index.html（让 React Router 接管路由）。
        # 关键：禁用缓存——否则浏览器用旧 index.html 指向已被构建清掉的旧 bundle 哈希，
        # 直接访问 /diagnostics 等深链时 JS 404 → 白屏。入口 HTML 不缓存，哈希资源照常缓存。
        return FileResponse(
            FRONTEND_DIST / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )


@app.exception_handler(Exception)
async def _unhandled(_req, exc: Exception):
    logging.exception("Unhandled error")
    return JSONResponse({"error": type(exc).__name__, "message": str(exc)}, status_code=500)
