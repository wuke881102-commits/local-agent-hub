"""本地目录功能的接口：浏览目录 / 列截图 / 预览图片 / 截图捕获会话。

内容生产复用通用任务接口（POST /api/tasks/run，agent_id="local-image"），此处只负责
目录与截图相关能力。
"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..services import capture_session, local_dir, local_extract, screenshot

router = APIRouter(prefix="/api/localdir", tags=["localdir"])


class DirBody(BaseModel):
    directory: str


@router.get("/browse")
async def browse(path: str | None = Query(default=None)) -> dict:
    try:
        return local_dir.browse(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/files")
async def files(dir: str = Query(...)) -> dict:
    try:
        return local_dir.list_files(dir)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/file")
async def file(path: str = Query(...)) -> FileResponse:
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=media)


@router.get("/extract")
async def extract(path: str = Query(...)) -> dict:
    """抽取本地文件（PDF/Word/Excel/PPT/截图除外）为 Markdown 文本，供「协作分发」等作素材。"""
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    kind = local_dir.kind_of(p.name)
    if not kind or kind == "image":
        raise HTTPException(400, "该类型不支持抽取为文本（仅支持 PDF/Word/Excel/PPT）")
    md = await asyncio.to_thread(local_extract.extract_markdown, p, kind)
    return {"name": p.name, "kind": kind, "markdown": md}


@router.get("/capture/status")
async def capture_status() -> dict:
    return capture_session.status()


@router.post("/capture/start")
async def capture_start(body: DirBody) -> dict:
    try:
        return capture_session.start(body.directory)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@router.post("/capture/stop")
async def capture_stop() -> dict:
    return capture_session.stop()


@router.post("/shot")
async def shot(body: DirBody) -> dict:
    """立即对当前活动窗口截一张（用于测试/手动补截）。"""
    try:
        path = screenshot.capture_to_dir(body.directory)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "path": str(path), "name": path.name}
