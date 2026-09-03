from __future__ import annotations

from fastapi import APIRouter

from ..agents import AGENT_REGISTRY

router = APIRouter(prefix="/api/agents", tags=["agents"])


# 每个 Agent 一个独立色相，侧栏圆点一眼可区分（避免多个同绿）。
AGENT_META = {
    "document-map":        {"icon": "map",    "color": "#2563EB", "entries": ["知识库治理", "内容生成"]},
    "index-enrich":        {"icon": "sparkle","color": "#DB2777", "entries": ["知识库治理"], "hidden": True},
    "knowledge-governance":{"icon": "shield", "color": "#0D9488", "entries": ["知识库治理"]},
    "html-page":           {"icon": "page",   "color": "#16A34A", "entries": ["内容生成"], "featured": True},
    "base-analysis":       {"icon": "table",  "color": "#F0A800", "entries": ["表格分析"]},
    "pdf-recognition":     {"icon": "scan",   "color": "#6A4DD4", "entries": ["PDF 识别"]},
    "meeting-minutes":     {"icon": "mic",    "color": "#EA580C", "entries": ["会议沉淀"]},
    "collab-dispatch":     {"icon": "send",   "color": "#C83A3A", "entries": ["协作分发"]},
    "aihot-models":        {"icon": "graph",  "color": "#4F46E5", "entries": ["AIHot 内容和模型"]},
    "aihot-news":          {"icon": "cloud",  "color": "#0891B2", "entries": ["AIHot 内容和模型"]},
}


@router.get("")
async def list_agents() -> dict:
    """侧栏与命令面板用的 Agent 列表。

    ``hidden`` 的不出现在这里，但**仍然注册着、仍然能跑**：目前只有
    「摘要标签回填」是这种——它已经改成刷新索引时自动触发（见
    routes/assets.py 的 ``_kick_enrich``），不该再让人手动去点一次。
    任务页 /task/index-enrich 保持可达：自动跑起来的那条任务要在
    「运行记录」里点得开、看得到进度和结果。
    """
    out = []
    for aid, a in AGENT_REGISTRY.items():
        meta = AGENT_META.get(aid, {})
        if meta.get("hidden"):
            continue
        out.append({
            "id": aid,
            "name": a.name,
            "desc": a.description,
            "writeback": a.writeback_allowed,
            "status": "ready",
            **meta,
        })
    return {"items": out}
