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
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/{task_id}/download")
async def download_task(task_id: str) -> FileResponse:
    t = await task_runner.get_task(task_id)
    if not t or not t.get("result_path"):
        raise HTTPException(404, "no draft")
    return FileResponse(t["result_path"], media_type="text/html",
                        filename=f"{task_id}.html")
