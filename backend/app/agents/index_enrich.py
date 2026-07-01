"""摘要 / 标签回填 Agent — 用 qwen3.6-flash 为本地索引补 summary/category/tags。

只读元信息（标题 / 类型 / 空间 / 负责人），不抓正文，分批并发调用，幂等：
默认只补"还没回填"的资产；force=True 时重跑全部。结果写回 asset 表的
summary / category / tags 三列，供搜索、列表预览与未来分面消费。

为何走 title-only：与"业务语义聚类"同口径——一次给几百篇逐篇抓正文太慢太贵，
而标题 + 空间 + 类型 + 负责人足以让模型写出可用的一句话定位与主题标签。
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

from .base import AgentContext, AgentResult, register_agent
from ..llm.prompts import build_index_enrich_prompt
from ..services import index_service


class IndexEnrichAgent:
    id = "index-enrich"
    name = "摘要标签回填 Agent"
    description = "用 qwen3.6-flash 读元信息为每篇资产生成一句话摘要、分类与主题标签，回填本地索引，让搜索与列表预览立刻变有用。"
    writeback_allowed = False

    BATCH = 20            # 每次喂给模型的文档数；20×~80token 远低于 max_tokens
    CONCURRENCY = 4       # 并发批次数

    async def run(self, ctx: AgentContext) -> AgentResult:
        inputs = ctx.inputs or {}
        force = bool(inputs.get("force"))
        try:
            limit = int(inputs.get("limit") or 2000)
        except (TypeError, ValueError):
            limit = 2000

        assets = await index_service.list_assets(limit=2000)
        total = len(assets)
        await ctx.log("info", f"本地索引共 {total} 条资产")

        todo = assets if force else [a for a in assets if not (a.get("summary") or "").strip()]
        already = total - len(todo)
        if not force and already:
            await ctx.log("info", f"{already} 条已回填，跳过；本次待处理 {len(todo)} 条")
        elif force:
            await ctx.log("info", f"强制重跑：覆盖全部 {len(todo)} 条")
        todo = todo[:limit]

        if not todo:
            await ctx.log("info", "没有需要回填的资产，全部已完成 ✅（如需覆盖重跑，请勾选「强制重跑」）")
            return AgentResult(task_id=ctx.task_id, status="done", payload=await _result_payload(0, total))

        fast = ctx.llm.text_model_fast
        batches = [todo[i:i + self.BATCH] for i in range(0, len(todo), self.BATCH)]
        await ctx.log("info", f"分 {len(batches)} 批（每批≤{self.BATCH}），并发 {self.CONCURRENCY}，调用 {fast} 生成摘要 / 分类 / 标签 …")

        sem = asyncio.Semaphore(self.CONCURRENCY)
        lock = asyncio.Lock()
        progress = {"batches": 0, "rows": 0, "failed": 0}

        async def _do_batch(bi: int, batch: list[dict]) -> None:
            numbered = [
                {"n": i + 1, "title": a.get("title"), "type": a.get("type"),
                 "space": a.get("space"), "owner": a.get("owner")}
                for i, a in enumerate(batch)
            ]
            system, user = build_index_enrich_prompt(numbered)
            async with sem:
                try:
                    raw = await ctx.llm.text_complete(
                        user, system=system, json_mode=True,
                        max_tokens=2500, timeout=70, retries=1, model=fast,
                    )
                except Exception as e:  # noqa: BLE001
                    async with lock:
                        progress["batches"] += 1
                        progress["failed"] += 1
                    await ctx.log("warn", f"第 {bi + 1} 批 LLM 调用失败，跳过：{e}")
                    return

            rows = _rows_from_response(raw, batch)
            # SQLite 单写者：把 DB 写入与计数放进锁里，避免并发 UPDATE 抢锁。
            async with lock:
                if rows:
                    await index_service.save_enrichment(rows)
                progress["batches"] += 1
                progress["rows"] += len(rows)
                await ctx.log(
                    "info",
                    f"进度 {progress['batches']}/{len(batches)} 批 · 已回填 {progress['rows']}/{len(todo)} 条",
                )

        await asyncio.gather(*[_do_batch(i, b) for i, b in enumerate(batches)])

        tail = f"，{progress['failed']} 批失败" if progress["failed"] else ""
        await ctx.log("info", f"回填完成：本次写入 {progress['rows']} 条{tail}")
        return AgentResult(task_id=ctx.task_id, status="done", payload=await _result_payload(progress["rows"], total))


# ── 解析模型输出 → 待写库的行 ──────────────────────────────────────


def _rows_from_response(raw: str, batch: list[dict]) -> list[dict]:
    parsed = _parse_json(raw) or {}
    items = parsed.get("items") or []
    rows: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(batch)):
            continue
        tags = it.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        clean_tags = [str(t).strip() for t in tags if str(t).strip()][:4]
        rows.append({
            "asset_id": batch[idx]["asset_id"],
            "summary": (it.get("summary") or "").strip(),
            "category": (it.get("category") or "").strip(),
            "tags": clean_tags,
        })
    return rows


def _parse_json(text: str) -> dict | None:
    """容错解析：去 fence → 直接 loads → 抓外层 {…} → 修复被截断的 items 数组。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[\s\S]*\}", s)
    candidate = m.group(0) if m else s
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 修复：在最后一个完整对象处闭合 items 数组（应对 max_tokens 截断）。
    pos = candidate.find('"items"')
    if pos > 0:
        bracket = candidate.find('[', pos)
        if bracket > 0:
            depth = 0
            last_complete = -1
            for i in range(bracket + 1, len(candidate)):
                ch = candidate[i]
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        last_complete = i
            if last_complete > 0:
                try:
                    return json.loads(candidate[: last_complete + 1] + "]}")
                except json.JSONDecodeError:
                    pass
    return None


# ── 结果统计（重新读库，供结果页展示覆盖率 / 分类分布 / 样例） ──────


async def _result_payload(enriched_now: int, total: int) -> dict:
    assets = await index_service.list_assets(limit=2000)
    have = [a for a in assets if (a.get("summary") or "").strip()]
    cat_counter: Counter[str] = Counter()
    for a in have:
        cat_counter[(a.get("category") or "未分类").strip() or "未分类"] += 1
    by_category = [{"name": k, "count": v} for k, v in cat_counter.most_common(20)]
    sample = [{
        "title": a.get("title"), "summary": a.get("summary"),
        "category": a.get("category"), "tags": a.get("tags") or [],
        "space": a.get("space"), "url": a.get("url"),
    } for a in have[:12]]
    return {
        "total": total,
        "enriched_total": len(have),
        "enriched_now": enriched_now,
        "coverage": round(len(have) / total * 100, 1) if total else 0,
        "by_category": by_category,
        "sample": sample,
    }


register_agent(IndexEnrichAgent())
