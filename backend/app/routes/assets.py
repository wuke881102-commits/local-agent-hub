from __future__ import annotations

import datetime as dt

from fastapi import APIRouter

from ..config import settings
from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..services import facets, governance, index_service

router = APIRouter(prefix="/api/assets", tags=["assets"])


async def _resolve_lark():
    lark = await get_lark()
    if isinstance(lark, LarkCLI) and not await lark.ping():
        if settings.enable_mock_fallback:
            return MockLarkCLI()
        raise RuntimeError("lark-cli unavailable")
    return lark


async def _my_open_id() -> str | None:
    try:
        lark = await get_lark()
        info = await lark.auth_status()
        return info.get("user_id") or info.get("open_id")
    except Exception:  # noqa: BLE001
        return None


@router.get("")
async def list_assets(
    type: str | None = None,
    q: str | None = None,
    type_exact: str | None = None,
    owner_id: str | None = None,
    space: str | None = None,
    created_year: str | None = None,
    category: str | None = None,
    origin: str | None = None,
    recency: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """本地索引列表（分页），支持来自"文档地图"分面的下钻过滤。

    type / type_exact / owner_id / space / category 走 SQL 列过滤（分页在 SQL 层做，
    返回 total 为过滤后总数）；origin / recency 是派生维度，需在内存里分类，故先取全量
    再过滤、再切片分页。返回 {items, total, stats}。
    """
    derived = bool(origin or recency)
    if derived:
        # 派生维度：取全量（索引规模有限），内存过滤后切片分页。
        items = await index_service.list_assets(
            asset_type=type, q=q, type_exact=type_exact,
            owner_id=owner_id, space=space, created_year=created_year,
            category=category, limit=5000,
        )
        my_id = await _my_open_id() if origin else None
        now = dt.datetime.now()
        if origin:
            items = [a for a in items if facets.origin_of(a, my_id) == origin]
        if recency:
            items = [a for a in items if facets.recency_of(a, now) == recency]
        total = len(items)
        items = items[offset:offset + limit]
    else:
        items = await index_service.list_assets(
            asset_type=type, q=q, type_exact=type_exact,
            owner_id=owner_id, space=space, created_year=created_year,
            category=category, limit=limit, offset=offset,
        )
        total = await index_service.count_assets(
            asset_type=type, q=q, type_exact=type_exact,
            owner_id=owner_id, space=space, created_year=created_year,
            category=category,
        )
    return {"items": items, "total": total, "stats": await index_service.stats()}


@router.post("/refresh")
async def refresh() -> dict:
    """Synchronous pull from lark-cli into the local index.

    No LLM call here — clustering belongs to the document-map agent. Keeping
    this endpoint fast and predictable matters because the dashboard refresh
    button calls it directly.
    """
    lark = await _resolve_lark()
    stats = await index_service.refresh(lark)
    return {"ok": True, "stats": stats, "index": await index_service.stats()}


@router.get("/stats")
async def stats() -> dict:
    return await index_service.stats()


@router.get("/filters")
async def filter_options() -> dict:
    """本地资产页筛选下拉选项（AI 分类 / 空间 / 负责人）。owner 标注 is_me。"""
    opts = await index_service.filter_options()
    my_id = await _my_open_id()
    for o in opts.get("owners", []):
        o["is_me"] = bool(my_id and o.get("owner_id") == my_id)
    return opts


@router.get("/governance")
async def governance_view(stale_days: int = 180, mine_only: bool = True) -> dict:
    """规则版陈旧三档分流（即时，无 LLM，不刷新索引）。

    与 document-map 同款"打开页面即出结果"模式：UI 在 /task/knowledge-governance
    页加载/改阈值时调它，瞬时拿到分流。LLM 逐条复核仍在 knowledge-governance Agent。
    """
    assets = await index_service.list_assets(limit=1000)
    applied = False
    if mine_only:
        my_id = await _my_open_id()
        if my_id:
            assets = [a for a in assets if (a.get("owner_id") or "") == my_id]
            applied = True
    triage = governance.compute_triage(assets, stale_days, mine_only=applied)
    return governance.to_payload(triage)


@router.get("/map")
async def map_view() -> dict:
    """Rule-based facets over the current local index. No LLM call, no task.

    UI uses this on /task/document-map page load so users see categorized data
    immediately. LLM clustering still lives in the document-map agent.
    """
    assets = await index_service.list_assets(limit=1000)
    my_open_id = await _my_open_id()
    payload = facets.compute_all(assets, my_open_id)
    payload["last_refreshed"] = (await index_service.stats()).get("last_refreshed")
    return payload
