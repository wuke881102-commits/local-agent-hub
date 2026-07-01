"""本地审计日志（PRD §12.3）。"""
from __future__ import annotations

import datetime as dt
import json
from ..db import get_db


async def write(actor: str | None, agent_id: str | None, action: str, target: str | None, outcome: str, details: dict | None = None) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_log (ts, actor, agent_id, action, target, outcome, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dt.datetime.now().isoformat(timespec="seconds"), actor, agent_id, action, target, outcome, json.dumps(details or {}, ensure_ascii=False)),
        )
        await db.commit()


async def recent(limit: int = 50) -> list[dict]:
    items: list[dict] = []
    async with get_db() as db:
        async with db.execute(
            "SELECT id, ts, actor, agent_id, action, target, outcome, details FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                items.append({
                    "id": row[0], "ts": row[1], "actor": row[2], "agent_id": row[3],
                    "action": row[4], "target": row[5], "outcome": row[6],
                    "details": json.loads(row[7] or "{}"),
                })
    return items
