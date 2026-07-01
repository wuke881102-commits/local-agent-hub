"""周期总结（周 / 月 / 年）路由。

GET  /api/summaries            —— 即时聚合（数字卡 / 主题分布 / 时间轴 / 陪伴天数），零 LLM。
POST /api/summaries/narrative  —— 按需用快档 LLM 生成一段叙述回顾 + Top 重点。

刻意不做成 agent：它是跨文档的只读聚合，不属于"对单个文档跑任务"的范式，
走独立轻路由比塞进 task_runner 更干净（无任务记录、无回写队列）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel

from ..feishu import get_lark
from ..llm import get_llm
from ..services import summary_service

router = APIRouter(prefix="/api/summaries", tags=["summaries"])


async def _my_open_id() -> str | None:
    try:
        lark = await get_lark()
        info = await lark.auth_status()
        return info.get("user_id") or info.get("open_id")
    except Exception:  # noqa: BLE001
        return None


def _norm_period(period: str | None) -> str:
    p = (period or "week").lower()
    return p if p in ("week", "month", "year") else "week"


@router.get("")
async def get_summary(period: str | None = None, offset: int = 0, local_dir: str | None = None) -> dict:
    """周期总结载荷。period=week|month|year，offset=0 当前期、-1 上一期。

    local_dir：可选，传入则把该本地目录中本期改动的文件并入统计 / 时间轴 / 回顾。
    """
    my_id = await _my_open_id()
    return await summary_service.build_summary(_norm_period(period), offset, my_id, local_dir=local_dir)


class NarrativeReq(BaseModel):
    period: str | None = None
    offset: int = 0
    local_dir: str | None = None


@router.post("/narrative")
async def narrative(req: NarrativeReq) -> dict:
    """用快档 LLM 生成一段叙述回顾 + 重点高亮（按需触发，不自动跑）。"""
    my_id = await _my_open_id()
    summary = await summary_service.build_summary(_norm_period(req.period), req.offset, my_id, local_dir=req.local_dir)
    items = summary_service.select_items_for_narrative(summary["timeline"])
    if not items:
        return {"narrative": "", "highlights": [], "range_label": summary["range_label"]}

    system, user = summary_service.build_narrative_prompt(summary["range_label"], items)
    llm = get_llm()
    try:
        raw = await llm.text_complete(
            user, system=system, json_mode=True,
            max_tokens=1200, timeout=60, retries=1, model=llm.text_model_fast,
        )
    except Exception as e:  # noqa: BLE001
        return {"narrative": "", "highlights": [], "range_label": summary["range_label"],
                "error": f"{type(e).__name__}: {str(e)[:160]}"}

    parsed: dict = {}
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        parsed = {}

    # 把模型挑出的 index 映射回真实条目（含 asset_id / url，前端可跳转）。
    highlights = []
    for h in (parsed.get("highlights") or []):
        try:
            idx = int(h.get("index"))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(items):
            src = items[idx - 1]
            highlights.append({
                "asset_id": src["asset_id"], "title": src["title"], "url": src["url"],
                "type": src["type"], "category": src["category"],
                "reason": (h.get("reason") or "").strip(),
            })

    return {
        "narrative": (parsed.get("narrative") or "").strip(),
        "highlights": highlights[:5],
        "range_label": summary["range_label"],
    }
