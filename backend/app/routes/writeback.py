from __future__ import annotations

import datetime as dt
import json
import re

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..db import get_db
from ..feishu import LarkCLI, MockLarkCLI, get_lark
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
            err = _explain_error(e)
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


# 常见 scope → 用户能看懂的中文名。命中才翻译，未命中原样显示 scope id。
_SCOPE_ZH = {
    "im:message": "发送消息",
    "im:message.send_as_user": "发送消息",
    "task:task:write": "创建任务",
    "docx:document:create": "创建文档",
}


def _explain_error(e: Exception) -> str:
    """把 lark-cli 的结构化报错翻成可读、可操作的中文。

    lark-cli 失败时 stderr 常带 ``{"ok":false,"error":{"type","message","hint"}}``。
    尤其 ``missing_scope`` 要直接告诉用户缺哪个 scope、怎么补授权——否则用户只看到
    "lark-cli exited 3" 完全无从下手。
    """
    stderr = getattr(e, "stderr", "") or ""
    m = re.search(r"\{[\s\S]*\}", stderr)
    if m:
        try:
            err = (json.loads(m.group(0)).get("error") or {})
            msg = (err.get("message") or "").strip()
            if err.get("type") == "missing_scope":
                sm = re.search(r"scope\(s\):\s*([\w:.\-,\s]+)", msg)
                scope = (sm.group(1).strip().rstrip(".") if sm else "")
                # 打包版用户没有命令行，绝不能让他们去敲 lark-cli auth login。
                # 引导到 App 左下角的「重新授权」按钮（startLogin force=True，会以最新最小
                # 权限集重新授权，补齐缺的 scope）。scope 译成中文名，更易懂。
                if scope:
                    friendly = "、".join(_SCOPE_ZH.get(s, s) for s in re.split(r"[,\s]+", scope) if s)
                    return (f"缺少飞书权限「{friendly}」。请点击左下角的「重新授权」按钮，"
                            f"在飞书授权页同意后再试。（技术细节：{scope}）")
                return f"缺少飞书权限：{msg}。请点击左下角「重新授权」后再试。"
            if msg:
                return msg
        except (json.JSONDecodeError, AttributeError):
            pass
    return str(e)[:200]


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
                results.append({"label": it.get("label"), "action_type": at, "ok": False, "error": _explain_error(e)})
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
