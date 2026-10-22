from __future__ import annotations

import datetime as dt
import json
import re

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..db import get_db
from ..feishu import LarkCLI, MockLarkCLI, explain_error, get_lark
from ..schemas import WritebackConfirmRequest, WritebackRejectRequest
from ..services import audit

router = APIRouter(prefix="/api/writeback", tags=["writeback"])


async def _resolve_lark():
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI()
    return lark


@router.get("/pending")
async def list_pending() -> dict:
    items = []
    async with get_db() as db:
        async with db.execute(
            "SELECT id, task_id, action_type, target, payload, status, created_at FROM writeback_queue WHERE status='pending' ORDER BY created_at DESC LIMIT 50"
        ) as cur:
            async for r in cur:
                items.append({
                    "id": r[0], "task_id": r[1], "action_type": r[2], "target": r[3],
                    "payload": json.loads(r[4] or "{}"), "status": r[5], "created_at": r[6],
                })
    return {"items": items}


@router.post("/confirm")
async def confirm(req: WritebackConfirmRequest) -> dict:
    async with get_db() as db:
        async with db.execute(
            "SELECT task_id, action_type, target, payload, status FROM writeback_queue WHERE id=?",
            (req.queue_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "writeback item not found")
        # pending = 首次确认；failed = 重试（如发消息缺权限失败、补授权后重发）。
        # executed / rejected 是终态，不可重复执行。
        if row[4] not in ("pending", "failed"):
            raise HTTPException(409, f"already {row[4]}")
        task_id, action_type, target, payload_json, _ = row
        payload = json.loads(payload_json or "{}")
        if req.edits:
            payload.update(req.edits)

        lark = await _resolve_lark()
        now = dt.datetime.now().isoformat(timespec="seconds")
        try:
            result = await _execute(action_type, payload, lark)
            await db.execute(
                "UPDATE writeback_queue SET status='executed', confirmed_at=?, executed_at=?, result=? WHERE id=?",
                (now, now, json.dumps(result, ensure_ascii=False), req.queue_id),
            )
            await db.commit()
            await audit.write(actor=None, agent_id=None, action=f"writeback:{action_type}",
                              target=str(target), outcome="executed",
                              details={"queue_id": req.queue_id, "result": result})
            return {"ok": True, "result": result}
        except Exception as e:  # noqa: BLE001
            err = explain_error(e)
            await db.execute(
                "UPDATE writeback_queue SET status='failed', confirmed_at=?, result=? WHERE id=?",
                (now, json.dumps({"error": err}, ensure_ascii=False), req.queue_id),
            )
            await db.commit()
            await audit.write(actor=None, agent_id=None, action=f"writeback:{action_type}",
                              target=str(target), outcome="failed", details={"error": err})
            raise HTTPException(500, f"写回执行失败：{err}")


@router.post("/reject")
async def reject(req: WritebackRejectRequest) -> dict:
    async with get_db() as db:
        async with db.execute("SELECT status, action_type, target FROM writeback_queue WHERE id=?", (req.queue_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "writeback item not found")
        if row[0] != "pending":
            raise HTTPException(409, f"already {row[0]}")
        await db.execute(
            "UPDATE writeback_queue SET status='rejected', confirmed_at=?, result=? WHERE id=?",
            (dt.datetime.now().isoformat(timespec="seconds"),
             json.dumps({"reason": req.reason or ""}, ensure_ascii=False), req.queue_id),
        )
        await db.commit()
        await audit.write(actor=None, agent_id=None, action=f"writeback:{row[1]}",
                          target=str(row[2]), outcome="rejected", details={"reason": req.reason})
    return {"ok": True}


async def _execute(action_type: str, payload: dict, lark) -> dict:
    if action_type == "batch_dispatch":
        # 一条提议携带 N 个子动作（建任务 / 发消息），逐个执行，单项失败不影响其它。
        items = payload.get("items") or []
        results: list[dict] = []
        ok = 0
        for it in items:
            at = it.get("action_type")
            pl = it.get("payload") or {}
            try:
                r = await _execute(at, pl, lark)
                results.append({"label": it.get("label"), "action_type": at, "ok": True, "result": r})
                ok += 1
            except Exception as e:  # noqa: BLE001
                results.append({"label": it.get("label"), "action_type": at, "ok": False, "error": explain_error(e)})
        # 全部失败才算整体失败（让确认弹窗走错误路径）；部分成功仍标记完成并展示明细。
        if items and ok == 0:
            first = next((r.get("error") for r in results if not r["ok"]), "未知错误")
            raise RuntimeError(f"全部分发失败：{first}")
        return {"dispatched": len(items), "ok_count": ok, "fail_count": len(items) - ok, "results": results}
    if action_type == "create_doc":
        return await lark.docs_create_markdown(
            title=payload.get("title", "未命名草稿"),
            content=payload.get("content_markdown", ""),
            folder_token=payload.get("folder_token"),
        )
    if action_type == "send_im":
        return await lark.im_send(
            chat_id=payload.get("chat_id", ""),
            text=payload.get("text", ""),
            markdown=bool(payload.get("markdown")),
            dry_run=False,
        )
    if action_type == "create_task":
        return await lark.task_create(
            title=payload.get("title", "未命名任务"),
            due=payload.get("due"),
            description=payload.get("description"),
            dry_run=False,
        )
    raise NotImplementedError(f"未支持的写回类型：{action_type}")
