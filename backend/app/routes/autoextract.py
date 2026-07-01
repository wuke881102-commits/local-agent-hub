"""「自动化提炼」场景接口：开/停定时截图提炼、立即提炼、提炼记录、截图列表。

截图落在应用私有目录（settings.captures_path），与「内容生成」的文件选择器隔离。
"""
from __future__ import annotations

import os
import sys

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import auto_extract

router = APIRouter(prefix="/api/autoextract", tags=["autoextract"])


class StartBody(BaseModel):
    interval_min: int = 15


@router.get("/status")
async def status() -> dict:
    return auto_extract.status()


@router.post("/start")
async def start(body: StartBody) -> dict:
    try:
        return await auto_extract.start(body.interval_min)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@router.post("/stop")
async def stop() -> dict:
    return await auto_extract.stop()


@router.post("/distill")
async def distill() -> dict:
    try:
        return await auto_extract.distill_now()
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("/digests")
async def digests(limit: int = 50) -> dict:
    return {"items": auto_extract.list_digests(limit=limit)}


@router.delete("/digests")
async def clear_digests() -> dict:
    n = auto_extract.clear_digests()
    return {"ok": True, "cleared": n}


@router.get("/shots")
async def shots(limit: int = 60, scope: str = "session") -> dict:
    return {"items": auto_extract.list_shots(limit=limit, scope=scope)}


@router.delete("/shots")
async def clear_shots(scope: str = "all") -> dict:
    n = auto_extract.clear_shots(scope=scope)
    return {"ok": True, "cleared": n}


@router.post("/reveal")
async def reveal() -> dict:
    """在系统文件管理器里打开截图所在的私有目录（方便用户查看 / 取用截图）。"""
    d = auto_extract.captures_dir()
    if not sys.platform.startswith("win"):
        raise HTTPException(400, "当前系统暂不支持一键打开文件夹。")
    try:
        os.startfile(str(d))  # type: ignore[attr-defined]  # noqa: S606 —— 本地单机应用，路径来自应用自身
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"无法打开文件夹：{e}")
    return {"ok": True, "directory": str(d)}
