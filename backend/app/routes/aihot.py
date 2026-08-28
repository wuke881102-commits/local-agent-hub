"""AIHot 内容和模型 —— 只读数据接口，给「打开就能看」的页面用。

和 agents/aihot_* 的分工：
  · 本模块：**纯取数**。抓 AIHOT → 归一化 → 返 JSON。不调大模型、不落草稿、不建任务，
    毫秒级（命中缓存）到两三秒（回源）。页面打开即渲染，就像看榜单页本身。
  · 两个 Agent：在同样的数据上**再加一层 AI**（选型建议 / 简报），慢、贵、产 HTML 草稿，
    是页面上的可选动作，不是看内容的前提。

归一化逻辑都在 services.aihot，与 Agent 共用同一套口径——同一个价格不会在页面和
草稿里显示成两个数。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from ..services import aihot

router = APIRouter(prefix="/api/aihot", tags=["aihot"])


def _err(e: Exception) -> HTTPException:
    """AihotError 的 message 是给用户看的中文，直接透出；其余归为 502。"""
    if isinstance(e, aihot.AihotError):
        return HTTPException(status_code=502, detail=str(e))
    return HTTPException(status_code=502, detail="AIHOT 取数失败：%s" % type(e).__name__)


@router.get("/leaderboard")
async def leaderboard(
    limit: int = Query(30, ge=1, le=100),
    provider: str = Query("", description="只看这个厂商（名字与榜单一致，如 Anthropic）"),
    ratio: float = Query(aihot.DEFAULT_IN_OUT_RATIO, ge=0, le=20,
                         description="性价比口径：假设输入:输出 ≈ 1:ratio"),
    compare_local: bool = Query(True, description="是否对照本机 .env 配的三档模型"),
    refresh: bool = Query(False, description="忽略本地缓存强制重抓"),
) -> dict:
    """模型榜全表。列与榜单页一致，另附本机计算的性价比。"""
    try:
        lb = await aihot.fetch_leaderboard(force=refresh)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e

    rows_all = aihot.leaderboard_rows(lb["entries"], ratio)
    rows = rows_all
    if provider.strip():
        want = provider.strip().lower()
        rows = [r for r in rows if r["provider"].lower() == want]
    meta = lb.get("meta") or {}
    return {
        "source_url": lb.get("source_url") or "",
        "updated_label": lb.get("updated_label") or "",
        "board_count": lb.get("board_count") or 0,
        "fetched_at": meta.get("fetched_at_iso") or "",
        "cached": bool(meta.get("cached")),
        "cache_age_s": meta.get("age_s") or 0,
        "policy": aihot.merge_policy(meta.get("policy")),
        "in_out_ratio": ratio,
        "total_on_board": len(rows_all),
        "providers": sorted({r["provider"] for r in rows_all if r["provider"]}),
        "rows": rows[:limit],
        "local_models": aihot.match_local_models(rows_all) if compare_local else [],
    }


@router.get("/model/{slug}")
async def model_detail(slug: str) -> dict:
    """单个模型的完整条目（含各证据家族分项得分）。给榜单行展开用。"""
    try:
        lb = await aihot.fetch_leaderboard()
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    for r in aihot.leaderboard_rows(lb["entries"]):
        if r["slug"] == slug:
            return {"row": r, "source_url": lb.get("source_url") or ""}
    raise HTTPException(404, "榜单里没有这个模型：%s" % slug)


@router.get("/news")
async def news(
    window: str = Query("24h", pattern="^(24h|7d)$"),
    mode: str = Query("selected", pattern="^(selected|all)$"),
    category: str = Query("", description="ai-models / ai-products / industry / paper / tip"),
    q: str = Query("", description="盯题关键词（≥2 字走站点检索）"),
    limit: int = Query(40, ge=1, le=100),
    include_hot: bool = Query(True, description="带当前热点榜并把榜上事件并入列表"),
    refresh: bool = Query(False),
) -> dict:
    """资讯列表 + 当前热点榜。页面打开就渲染这个，不经过大模型。"""
    jobs = [aihot.fetch_items(window=window, mode=mode, category=category,
                             q=q, limit=limit, force=refresh)]
    if include_hot:
        jobs.append(aihot.fetch_hot_topics(force=refresh))
    done = await asyncio.gather(*jobs, return_exceptions=True)

    if isinstance(done[0], Exception):
        raise _err(done[0])
    raw, meta = done[0]

    hot: list[dict] = []
    hot_err = ""
    if include_hot:
        if isinstance(done[1], Exception):
            # 热点榜挂了不该让整页空白——资讯列表照样给，页面上标一下就好。
            hot_err = str(done[1])[:200]
        else:
            hot, _m = done[1]

    items = aihot.dedupe_items([aihot.news_item(x) for x in raw
                                if (x.get("title") or "").strip()])
    added = aihot.merge_hot_topics(items, hot) if hot else 0
    items.sort(key=lambda it: (it.get("hot_rank") or 99,
                              -(it.get("score") or 0),
                              it.get("published_at") or ""))
    return {
        "source_url": aihot.base_url(),
        "window": window, "mode": mode, "category": category, "q": q,
        "fetched_at": meta.get("fetched_at_iso") or "",
        "cached": bool(meta.get("cached")),
        "cache_age_s": meta.get("age_s") or 0,
        "policy": aihot.merge_policy(meta.get("policy")),
        "categories": [{"id": k, "label": v} for k, v in aihot.CATEGORY_CN.items()],
        "items": items[:limit],
        "hot_merged": added,
        "hot_error": hot_err,
        "hot_topics": [{
            "rank": h.get("rank"),
            "title": (h.get("title") or "").strip(),
            "source_count": h.get("sourceCount"),
            "signal_count": h.get("signalCount"),
            "story_id": aihot.story_id_from_url((h.get("links") or {}).get("story") or ""),
            "story_url": (h.get("links") or {}).get("story") or "",
            "url": (h.get("links") or {}).get("original") or "",
            "latest_zh": aihot.short_dt(h.get("latestAt") or ""),
        } for h in hot],
    }


@router.get("/story/{public_id}")
async def story(public_id: str, refresh: bool = Query(False)) -> dict:
    """某个热点事件的报道时间线 + 站点综述。点热点条目时按需拉。"""
    try:
        st, meta = await aihot.fetch_story(public_id, force=refresh)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    return {"story": aihot.story_summary(st),
            "cached": bool(meta.get("cached")),
            "policy": aihot.merge_policy(meta.get("policy"))}


@router.get("/daily")
async def daily(refresh: bool = Query(False)) -> dict:
    """站点当日精编日报（每天 08:00 北京时间发布），按分栏原样展示。"""
    try:
        rep, meta = await aihot.fetch_daily_latest(force=refresh)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    return {
        "date": rep.get("date") or "",
        "generated_at": rep.get("generatedAt") or "",
        "url": (rep.get("links") or {}).get("aihot") or "",
        "lead": rep.get("lead") or "",
        "cached": bool(meta.get("cached")),
        "policy": aihot.merge_policy(meta.get("policy")),
        "sections": [{
            "label": (s.get("label") or "").strip(),
            "items": [aihot.news_item(x) for x in (s.get("items") or [])],
        } for s in (rep.get("sections") or [])],
        "flashes": [(x.get("title") or "").strip() for x in (rep.get("flashes") or [])],
    }
