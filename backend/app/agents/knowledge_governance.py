"""知识治理 Agent — 在规则三档分流（services.governance）之上叠加 LLM 复核。

规则分流逻辑与 /api/assets/governance 端点共用 services.governance，保证"页面即时
预览"和"LLM 复核"口径一致。本 Agent 额外用 qwen3.6-flash 逐批复核归档候选（给置信度
与理由、可把误判改回保留），并对重复/无主给处置建议。
"""
from __future__ import annotations

import asyncio
import json
import re

from .base import AgentContext, AgentResult, register_agent
from ..llm.prompts import build_knowledge_gov_prompt, build_stale_triage_prompt
from ..services import governance, index_service


class KnowledgeGovernanceAgent:
    id = "knowledge-governance"
    name = "知识治理 Agent"
    description = "失修（三档分流）/ 重复 / 无主检测，结合规则与 LLM 给出归档/合并/转交建议。"
    writeback_allowed = True  # 暂未自动写回，留接口；用户按清单在飞书手动处理。

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        stale_days: int = int(inputs.get("stale_days") or 180)
        skip_refresh = bool(inputs.get("skip_refresh"))
        mine_only = inputs.get("mine_only", True)

        if not skip_refresh:
            await ctx.log("info", "刷新本地索引 …")
            try:
                await index_service.refresh(ctx.lark, log=ctx.log)
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"索引刷新失败：{e}（继续基于已有数据扫描）")

        assets = await index_service.list_assets(limit=1000)

        mine_applied = False
        if mine_only:
            my_id = await _resolve_my_open_id(ctx)
            if my_id:
                before = len(assets)
                assets = [a for a in assets if (a.get("owner_id") or "") == my_id]
                mine_applied = True
                await ctx.log("info", f"仅治理我创建的文档：{len(assets)}/{before} 篇（owner = 当前用户）")
            else:
                await ctx.log("warn", "无法识别当前用户身份，本次改为扫描全部文档")
        await ctx.log("info", f"本次扫描 {len(assets)} 条资产，开始分析")

        # ── 规则三档分流（与端点同源）──
        triage = governance.compute_triage(assets, stale_days, mine_only=mine_applied)
        buckets = triage["buckets"]
        await ctx.log(
            "info",
            f"失修分流（规则）：建议归档 {len(buckets['archive'])} · 长青参考 {len(buckets['evergreen'])} · 待复核 {len(buckets['review'])}",
        )
        await ctx.log("info", f"无主 {len(triage['no_owner'])} 篇 · 重复嫌疑 {len(triage['dup_groups'])} 组")

        # ── LLM 逐批复核归档候选（给置信度/理由，可改判为保留）──
        # 上限 500：超大库里超出部分仍按规则判为归档、但不经 LLM 复核（逐批调用，越多越慢/越贵）。
        to_llm = buckets["archive"][:500]
        if to_llm:
            await ctx.log("info", f"调用 {ctx.llm.text_model_fast} 复核 {len(to_llm)} 篇归档候选 …")
            await _llm_triage(ctx, to_llm, buckets)
        buckets["archive"].sort(key=lambda x: (governance.CONF_RANK.get(x.get("confidence"), 3), x.get("updated") or ""))
        buckets["review"].sort(key=lambda x: x.get("updated") or "")

        # ── LLM 重复/无主处置建议 ──
        recommendations: dict = {}
        if triage["no_owner"] or triage["dup_groups"]:
            fast = ctx.llm.text_model_fast
            await ctx.log("info", f"调用 {fast} 生成重复/无主处置建议 …")
            try:
                system, user = build_knowledge_gov_prompt(
                    stale_days=stale_days, stale=[],
                    dups=triage["dup_groups"][:25], no_owner=triage["no_owner"][:60],
                )
                raw = await ctx.llm.text_complete(
                    user, system=system, json_mode=True,
                    max_tokens=3000, timeout=70, retries=1, model=fast,
                )
                recommendations = _safe_parse_json(raw) or {}
                await ctx.log("info", "LLM 建议已返回")
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"LLM 建议生成失败：{e}（仅返回扫描结果）")

        payload = governance.to_payload(triage, recommendations)
        await ctx.log(
            "info",
            f"复核后：建议归档 {payload['metrics']['archive_count']} · 待复核 {payload['metrics']['review_count']}",
        )
        return AgentResult(task_id=ctx.task_id, status="done", payload=payload)


async def _resolve_my_open_id(ctx) -> str | None:
    try:
        info = await ctx.lark.auth_status()
        return info.get("user_id") or info.get("open_id")
    except Exception:  # noqa: BLE001
        return None


async def _llm_triage(ctx, cands: list[dict], buckets: dict) -> None:
    """分批复核归档候选：更新每条的 confidence/reason；LLM 判为 keep 的挪到待复核。

    cands 是 buckets['archive'] 里对象的引用，原地改写即生效。
    """
    BATCH = 25
    batches = [cands[i:i + BATCH] for i in range(0, len(cands), BATCH)]
    sem = asyncio.Semaphore(4)
    lock = asyncio.Lock()
    moved = {"updated": 0, "keep": 0}

    async def _do(bi: int, batch: list[dict]) -> None:
        numbered = [
            {"n": i + 1, "title": a.get("title"), "category": a.get("category"),
             "updated": a.get("updated"), "summary": a.get("summary")}
            for i, a in enumerate(batch)
        ]
        system, user = build_stale_triage_prompt(numbered)
        async with sem:
            try:
                raw = await ctx.llm.text_complete(
                    user, system=system, json_mode=True,
                    max_tokens=2000, timeout=70, retries=1, model=ctx.llm.text_model_fast,
                )
            except Exception as e:  # noqa: BLE001
                await ctx.log("warn", f"第 {bi + 1} 批复核失败，保留规则判断：{e}")
                return
        parsed = _safe_parse_json(raw) or {}
        async with lock:
            for it in parsed.get("items") or []:
                if not isinstance(it, dict):
                    continue
                try:
                    idx = int(it.get("n")) - 1
                except (TypeError, ValueError):
                    continue
                if not (0 <= idx < len(batch)):
                    continue
                a = batch[idx]
                conf = (it.get("confidence") or "").lower()
                if conf in governance.CONF_RANK:
                    a["confidence"] = conf
                if it.get("reason"):
                    a["reason"] = str(it["reason"]).strip()
                if (it.get("action") or "").lower() == "keep":
                    a["_demote"] = True
                    moved["keep"] += 1
                moved["updated"] += 1

    await asyncio.gather(*[_do(i, b) for i, b in enumerate(batches)])

    if moved["keep"]:
        demoted = [a for a in buckets["archive"] if a.get("_demote")]
        buckets["archive"] = [a for a in buckets["archive"] if not a.get("_demote")]
        for a in demoted:
            a.pop("_demote", None)
            buckets["review"].append(a)
    await ctx.log(
        "info",
        f"复核完成：{moved['updated']} 篇更新理由/置信度，其中 {moved['keep']} 篇改判为保留（移入待复核）",
    )


def _safe_parse_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


register_agent(KnowledgeGovernanceAgent())
