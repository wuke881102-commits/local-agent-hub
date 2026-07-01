from __future__ import annotations

from fastapi import APIRouter

from ..agents import AGENT_REGISTRY

router = APIRouter(prefix="/api/agents", tags=["agents"])


# 每个 Agent 一个独立色相，侧栏圆点一眼可区分（避免多个同绿）。
AGENT_META = {
    "document-map":        {"icon": "map",    "color": "#2563EB", "entries": ["知识库治理", "内容生成"]},
    "index-enrich":        {"icon": "sparkle","color": "#DB2777", "entries": ["知识库治理"]},
    "knowledge-governance":{"icon": "shield", "color": "#0D9488", "entries": ["知识库治理"]},
    "html-page":           {"icon": "page",   "color": "#16A34A", "entries": ["内容生成"], "featured": True},
    "base-analysis":       {"icon": "table",  "color": "#F0A800", "entries": ["表格分析"]},
    "pdf-recognition":     {"icon": "scan",   "color": "#6A4DD4", "entries": ["PDF 识别"]},
    "meeting-minutes":     {"icon": "mic",    "color": "#EA580C", "entries": ["会议沉淀"]},
    "collab-dispatch":     {"icon": "send",   "color": "#C83A3A", "entries": ["协作分发"]},
}


@router.get("")
async def list_agents() -> dict:
    out = []
    for aid, a in AGENT_REGISTRY.items():
        meta = AGENT_META.get(aid, {})
        out.append({
            "id": aid,
            "name": a.name,
            "desc": a.description,
            "writeback": a.writeback_allowed,
            "status": "ready",
            **meta,
        })
    return {"items": out}
