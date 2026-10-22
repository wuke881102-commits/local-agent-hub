from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from ..schemas import RunTaskRequest, RunTaskResponse
from ..services import task_runner

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("/run", response_model=RunTaskResponse)
async def run_task(req: RunTaskRequest) -> RunTaskResponse:
    try:
        tid = await task_runner.submit(req.agent_id, req.inputs, scene=req.scene)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RunTaskResponse(task_id=tid)


@router.post("/{task_id}/retry", response_model=RunTaskResponse)
async def retry_task(task_id: str) -> RunTaskResponse:
    """用原任务的 agent / 输入 / 场景重跑一遍（生成新任务）。

    供"运行记录"里对失败任务一键重试用。写回失败（已生成内容、仅发送失败）不走这里——
    那种直接在确认弹窗里对 failed 的 writeback 项重新确认即可，无需重新生成。
    """
    t = await task_runner.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    try:
        tid = await task_runner.submit(t["agent_id"], t.get("inputs") or {}, scene=t.get("scene") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RunTaskResponse(task_id=tid)


@router.get("")
async def list_tasks(limit: int = 30) -> dict:
    return {"items": await task_runner.list_recent(limit=limit)}


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict:
    t = await task_runner.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> dict:
    try:
        ok = await task_runner.delete_task(task_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if not ok:
        raise HTTPException(404, "task not found")
    return {"ok": True, "deleted": task_id}


@router.get("/{task_id}/stream")
async def stream_task(task_id: str) -> StreamingResponse:
    async def gen():
        async for entry in task_runner.stream(task_id):
            yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


@router.get("/{task_id}/preview")
async def preview_task(task_id: str) -> HTMLResponse:
    t = await task_runner.get_task(task_id)
    if not t or not t.get("result_path"):
        raise HTTPException(404, "preview not available")
    path = Path(t["result_path"])
    if not path.exists():
        raise HTTPException(404, "preview file missing")
    if path.suffix.lower() not in (".html", ".htm"):
        # 预览只认 HTML。原先对任何产物都 read_text(utf-8)，.pptx 是二进制，
        # 走到这里就是 UnicodeDecodeError → 500，而前端只会看到一块白。
        raise HTTPException(415, f"{path.suffix} 产物不支持网页预览，请下载后打开")
    return HTMLResponse(path.read_text(encoding="utf-8"))


# 产物类型按扩展名决定。原先这里硬编码 text/html + .html：.pptx 走过来会被
# 浏览器当网页打开，下下来的文件名也是错的。
_DOWNLOAD_TYPES = {
    ".html": "text/html",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _download_meta(task_id: str, result_path: str) -> tuple[str, str]:
    """返回 (media_type, 下载文件名)。认不出的扩展名给 octet-stream —— 让浏览器
    下载，而不是猜错类型当网页渲染。"""
    ext = Path(result_path).suffix.lower()
    return _DOWNLOAD_TYPES.get(ext, "application/octet-stream"), f"{task_id}{ext}"


@router.get("/{task_id}/download")
async def download_task(task_id: str) -> FileResponse:
    t = await task_runner.get_task(task_id)
    if not t or not t.get("result_path"):
        raise HTTPException(404, "no draft")
    media, filename = _download_meta(task_id, t["result_path"])
    return FileResponse(t["result_path"], media_type=media, filename=filename)
