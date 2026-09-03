from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter

from ..config import settings
from ..feishu import LarkCLI, MockLarkCLI, get_lark
from ..llm import get_llm
from ..services import facets, governance, index_service, task_runner

router = APIRouter(prefix="/api/assets", tags=["assets"])

log = logging.getLogger("assets")

# 刷新索引后顺带触发的回填 Agent。
_ENRICH_AGENT = "index-enrich"


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


async def _kick_enrich() -> dict:
    """刷新索引之后，顺手把「摘要标签回填」跑起来。

    ## 为什么在这里，而不是在按钮那边

    刷新索引有三个入口（工作台 / 飞书文档 / 历史总结）。挂在按钮上就要挂三次，
    第四个入口出现时必然漏掉一个。挂在这里，凡是刷新过索引的路径都自动带上。

    ## 为什么不等它跑完

    ``task_runner.submit`` 是即发即走的：写一行 task_run，起一个 asyncio 任务，
    立刻返回 task_id。刷新接口因此仍然是「拉完就返回」，几百条资产的模型调用在
    后台跑，进度和结果去「运行记录」里看。

    这条接口原本刻意不碰模型（"No LLM call here"），现在破例了——但破得干净：
    **这里依然没有 await 任何模型调用**，只是排了个后台任务。

    ## 四种不起的情况，每种都如实说出来

    静默不做事是最糟的：用户会以为回填生效了，其实一次也没跑过。
    """
    # 文字模型是 mock 时坚决不跑：回填会把假摘要写进索引，而 Agent 判断「是否已回填」
    # 靠的就是 summary 非空——一旦写进去，以后正常模式下也不会再补，假数据永久留下。
    try:
        if getattr(get_llm(), "mock", False):
            return {"started": False, "reason": "mock", "pending": 0, "task_id": ""}
    except Exception:  # noqa: BLE001
        return {"started": False, "reason": "mock", "pending": 0, "task_id": ""}

    try:
        pending = await index_service.count_unenriched()
    except Exception as e:  # noqa: BLE001
        log.warning("统计待回填资产失败：%s", e)
        return {"started": False, "reason": "error", "pending": 0, "task_id": "", "error": str(e)[:200]}

    if not pending:
        return {"started": False, "reason": "nothing", "pending": 0, "task_id": ""}

    running = await task_runner.running_task_id(_ENRICH_AGENT)
    if running:
        return {"started": False, "reason": "running", "pending": pending, "task_id": running}

    try:
        # force 不传：只补 summary 为空的，跑过的不重跑。刷新是高频动作，
        # 每次全量重跑既贵又会把已有摘要洗掉。要覆盖重跑仍走 Agent 页手动勾选。
        task_id = await task_runner.submit(_ENRICH_AGENT, {}, scene="知识库治理")
    except Exception as e:  # noqa: BLE001
        log.warning("触发摘要标签回填失败：%s", e)
        return {"started": False, "reason": "error", "pending": pending, "task_id": "", "error": str(e)[:200]}

    log.info("索引刷新完成，已后台触发摘要标签回填：%s（待回填 %d 条）", task_id, pending)
    return {"started": True, "reason": "", "pending": pending, "task_id": task_id}


@router.post("/refresh")
async def refresh() -> dict:
    """Synchronous pull from lark-cli into the local index, then kick off enrichment.

    The pull itself still makes no LLM call — clustering belongs to the
    document-map agent, and keeping this endpoint fast matters because the
    dashboard refresh button calls it directly. Enrichment is *queued*, not
    awaited: see ``_kick_enrich``.
    """
    lark = await _resolve_lark()
    stats = await index_service.refresh(lark)
    enrich = await _kick_enrich()
    return {"ok": True, "stats": stats, "index": await index_service.stats(), "enrich": enrich}


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
