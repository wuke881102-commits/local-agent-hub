"""文档地图 Agent — 纯规则维度（瞬时计算）。

来源 / 类型 / 所有者 / 活跃度 / 创建时段 / 空间 / AI 分类，全部由 ``facets.compute_all``
在内存里算出，可点击下钻。

旧的「业务语义聚类」（一次性 LLM 关键词聚类）已移除：给文档分类这件事，现在由
「摘要 / 标签回填」生成的固定 AI 分类承担——覆盖全量、单一明确、已持久化、可下钻，
比覆盖不全的关键词聚类更可靠，且无需每次等 25–40 秒。
"""
from __future__ import annotations

from .base import AgentContext, AgentResult, register_agent
from ..services import facets, index_service


class DocumentMapAgent:
    id = "document-map"
    name = "文档地图 Agent"
    description = "刷新本地索引并按来源/类型/所有者/活跃度/创建时段/空间/AI 分类自动分组（瞬时，规则计算，可点击下钻）。"
    writeback_allowed = False

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        skip_refresh = bool(inputs.get("skip_refresh"))

        if not skip_refresh:
            await ctx.log("info", "刷新本地索引 …")
            try:
                stats = await index_service.refresh(ctx.lark, log=ctx.log)
                await ctx.log(
                    "info",
                    f"索引：docs={stats['docs']} wiki={stats['wiki']} base={stats['base']} "
                    f"sheet={stats.get('sheet', 0)} meeting={stats['meeting']}",
                )
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"索引刷新部分失败：{e}（继续基于已有数据出图）")
        else:
            await ctx.log("info", "跳过索引刷新，直接基于现有数据生成地图")

        assets = await index_service.list_assets(limit=2000)
        await ctx.log("info", f"本地索引共 {len(assets)} 条资产")

        my_open_id = await _resolve_my_open_id(ctx)
        await ctx.log("info", f"识别当前用户 open_id：{(my_open_id or '未取得')[:20]}…")

        payload = facets.compute_all(assets, my_open_id)
        payload["last_refreshed"] = (await index_service.stats()).get("last_refreshed")
        await ctx.log(
            "info",
            f"规则维度：来源 {len(payload['by_origin'])} · 类型 {len(payload['by_type'])} · "
            f"所有者 {len(payload['by_owner'])} · 活跃度 {len(payload['by_recency'])} · "
            f"创建时段 {len(payload['by_created'])} · 空间 {len(payload['by_space'])} · "
            f"AI 分类 {len(payload['by_category'])}",
        )

        return AgentResult(task_id=ctx.task_id, status="done", payload=payload)


async def _resolve_my_open_id(ctx) -> str | None:
    try:
        info = await ctx.lark.auth_status()
        return info.get("user_id") or info.get("open_id")
    except Exception:  # noqa: BLE001
        return None


register_agent(DocumentMapAgent())
