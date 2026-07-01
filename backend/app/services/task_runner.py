"""Agent 任务编排器 + 流式日志（SSE 友好）。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import uuid
from typing import Any, AsyncIterator

from ..agents import AgentContext, AgentResult, get_agent
from ..db import get_db
from ..config import settings
from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..llm import get_llm
from . import audit, index_service


# 任务「对象」列：把 token / 飞书链接归一成可查的 token，再换成资产标题显示。
_TOKEN_IN_URL = re.compile(r"/([A-Za-z0-9]{12,})(?:[?#/]|$)")


def _norm_token(s: str) -> str:
    m = _TOKEN_IN_URL.search(s or "")
    return m.group(1) if m else (s or "").strip()


# 内存中保留运行中的任务通道
_task_channels: dict[str, asyncio.Queue[dict]] = {}
_task_results: dict[str, AgentResult] = {}


async def _resolve_lark() -> Any:
    """根据 CLI 可用性决定使用真实 LarkCLI 还是 Mock。"""
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI()
        raise RuntimeError("lark-cli 不可用，且 ENABLE_MOCK_FALLBACK=false。")
    return lark


async def reap_orphans() -> int:
    """启动时调用：把上次进程残留的 'running' 任务标记为 failed。

    后端无 --reload，重启（或崩溃）会杀掉所有在途的 asyncio 任务，但 DB 行仍停在
    'running'，前端会一直显示 RUNNING。开机时统一收尸，避免出现永远转圈的僵尸任务。
    单实例本地部署，开机时不可能有真正在跑的任务，故可安全清理。
    """
    now = dt.datetime.now().isoformat(timespec="seconds")
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE task_run SET status='failed', "
            "error=COALESCE(NULLIF(error,''),'后端重启中断，任务未完成（请重新运行）'), "
            "finished_at=? WHERE status='running'",
            (now,),
        )
        await db.commit()
        return cur.rowcount or 0


async def submit(agent_id: str, inputs: dict[str, Any], scene: str | None = None) -> str:
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError(f"未注册的 agent: {agent_id}")
    task_id = "t-" + uuid.uuid4().hex[:8]
    raw_target = (inputs.get("title") or inputs.get("doc_token")
                  or inputs.get("asset_id") or inputs.get("file_token") or "")
    target = raw_target
    # 没有显式 title 时，把 token 换成资产标题（文件名），让「最近任务」可读。
    if not inputs.get("title") and raw_target:
        try:
            title = await index_service.get_asset_title(_norm_token(raw_target))
            if title:
                target = title
        except Exception:  # noqa: BLE001
            pass
    # 无文档/资产对象时（如「协作分发」以 source_task_id / content 为输入），
    # 退而用上游任务的对象、或内容首行，避免「对象」列空着只显示「—」。
    if not target:
        src_id = (inputs.get("source_task_id") or "").strip()
        if src_id:
            try:
                src = await get_task(src_id)
                if src and src.get("target") and src["target"] != "—":
                    target = src["target"]
            except Exception:  # noqa: BLE001
                pass
    if not target:
        content = (inputs.get("content") or "").strip()
        if content:
            target = content.splitlines()[0][:30]
    if not target:
        target = "—"

    now = dt.datetime.now().isoformat(timespec="seconds")
    async with get_db() as db:
        await db.execute(
            "INSERT INTO task_run (id, agent_id, scene, target, inputs, status, started_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, agent_id, scene or "", target, json.dumps(inputs, ensure_ascii=False), "running", now),
        )
        await db.commit()

    q: asyncio.Queue[dict] = asyncio.Queue()
    _task_channels[task_id] = q

    asyncio.create_task(_run(task_id, agent, inputs))
    return task_id


async def _run(task_id: str, agent, inputs: dict[str, Any]) -> None:
    q = _task_channels[task_id]
    lark = await _resolve_lark()
    llm = get_llm()

    async def emit(level: str, message: str) -> None:
        entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "level": level, "message": message}
        await q.put(entry)
        async with get_db() as db:
            await db.execute(
                "INSERT INTO task_log (task_id, ts, level, message) VALUES (?,?,?,?)",
                (task_id, entry["ts"], level, message),
            )
            await db.commit()

    ctx = AgentContext(task_id=task_id, agent_id=agent.id, inputs=inputs, lark=lark, llm=llm, emit=emit)
    try:
        result = await agent.run(ctx)
    except Exception as e:  # noqa: BLE001
        await emit("error", f"Agent 抛出异常：{type(e).__name__}: {e}")
        result = AgentResult(task_id=task_id, status="failed", error=str(e))

    _task_results[task_id] = result

    async with get_db() as db:
        await db.execute(
            "UPDATE task_run SET status=?, finished_at=?, result_path=?, error=?, payload=? WHERE id=?",
            (
                result.status,
                dt.datetime.now().isoformat(timespec="seconds"),
                result.result_path,
                result.error,
                json.dumps(result.payload, ensure_ascii=False) if result.payload else None,
                task_id,
            ),
        )
        # writeback proposal 入队
        if result.writeback_proposal:
            wb_id = "w-" + uuid.uuid4().hex[:8]
            wp = result.writeback_proposal
            await db.execute(
                "INSERT INTO writeback_queue (id, task_id, action_type, target, payload, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (wb_id, task_id, wp.get("action_type", "create_doc"), wp.get("target", ""),
                 json.dumps(wp, ensure_ascii=False), "pending", dt.datetime.now().isoformat(timespec="seconds")),
            )
        await db.commit()

    await audit.write(
        actor=None, agent_id=agent.id, action="agent_run",
        target=task_id, outcome=result.status,
        details={"inputs": inputs, "error": result.error},
    )
    await q.put({"_done": True, "status": result.status})


async def stream(task_id: str) -> AsyncIterator[dict]:
    """SSE 数据源：直到收到 _done 才结束。"""
    # 先 replay 已经入库的日志
    async with get_db() as db:
        async with db.execute(
            "SELECT ts, level, message FROM task_log WHERE task_id=? ORDER BY id", (task_id,),
        ) as cur:
            async for r in cur:
                yield {"ts": r[0], "level": r[1], "message": r[2]}

    q = _task_channels.get(task_id)
    if not q:
        # 没有内存通道 = 任务已结束（或在别的进程跑完）。补发一个 _done，
        # 让前端把这次连接当作正常收尾，而不是误判为「连接中断」。
        status = "done"
        async with get_db() as db:
            async with db.execute("SELECT status FROM task_run WHERE id=?", (task_id,)) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    status = row[0]
        yield {"_done": True, "status": status}
        return
    while True:
        try:
            entry = await asyncio.wait_for(q.get(), timeout=60)
        except asyncio.TimeoutError:
            yield {"_keepalive": True}
            continue
        if entry.get("_done"):
            yield {"_done": True, "status": entry.get("status")}
            return
        yield entry


def get_result(task_id: str) -> AgentResult | None:
    return _task_results.get(task_id)


async def list_recent(limit: int = 30) -> list[dict]:
    out: list[dict] = []
    async with get_db() as db:
        async with db.execute(
            """SELECT t.id, t.agent_id, t.scene, t.target, t.status, t.started_at, t.finished_at, t.error,
                      (SELECT status FROM writeback_queue w WHERE w.task_id=t.id ORDER BY w.created_at DESC LIMIT 1) AS wb_status
                 FROM task_run t ORDER BY t.started_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            async for r in cur:
                out.append({
                    "id": r[0], "agent_id": r[1], "scene": r[2], "target": r[3], "status": r[4],
                    "started_at": r[5], "finished_at": r[6], "error": r[7],
                    "writeback": r[8] or "—",
                })
    # 历史任务的 target 可能仍是 token：批量换成资产标题（找不到的保持原样）。
    norm = {o["id"]: _norm_token(o["target"]) for o in out if o["target"] and o["target"] != "—"}
    if norm:
        try:
            titles = await index_service.get_titles_for(list(norm.values()))
            for o in out:
                nt = norm.get(o["id"])
                if nt and nt in titles:
                    o["target"] = titles[nt]
        except Exception:  # noqa: BLE001
            pass
    return out


async def delete_task(task_id: str) -> bool:
    """删除一个任务及其日志、写回队列记录。

    返回 True 表示已删除；False 表示任务不存在。运行中的任务不允许删除
    （后台协程仍会写日志/收尾），抛 ValueError 由路由转成 409。
    """
    async with get_db() as db:
        async with db.execute("SELECT status FROM task_run WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        if row[0] == "running":
            raise ValueError("任务正在运行，无法删除；请等待其结束后再试。")
        await db.execute("DELETE FROM writeback_queue WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM task_log WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM task_run WHERE id=?", (task_id,))
        await db.commit()
    # 清理内存中的运行态缓存
    _task_channels.pop(task_id, None)
    _task_results.pop(task_id, None)
    return True


async def get_task(task_id: str) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT id, agent_id, scene, target, inputs, status, started_at, finished_at, result_path, error, payload FROM task_run WHERE id=?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            wb = None
            async with db.execute("SELECT id, action_type, target, payload, status FROM writeback_queue WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)) as cur2:
                wb_row = await cur2.fetchone()
                if wb_row:
                    wb = {"id": wb_row[0], "action_type": wb_row[1], "target": wb_row[2],
                          "payload": json.loads(wb_row[3] or "{}"), "status": wb_row[4]}
            return {
                "id": row[0], "agent_id": row[1], "scene": row[2], "target": row[3],
                "inputs": json.loads(row[4] or "{}"),
                "status": row[5], "started_at": row[6], "finished_at": row[7],
                "result_path": row[8], "error": row[9],
                "payload": json.loads(row[10]) if row[10] else None,
                "writeback": wb,
            }
